"""Pipeline orchestration.

Phase 2 implements the collect -> dedupe -> persist -> prefilter portion:

* iterate the enabled sources, isolating per-source failures;
* normalize and persist each job through :meth:`JobRepository.upsert_job`, which is
  idempotent on ``(source, external_id)`` and ``url`` (running twice adds nothing);
* run the deterministic prefilter and mark rejected jobs ``filtered_out`` while
  leaving the rest ``new`` for later phases.

Later phases extend this module (analysis, scoring, Telegram, sending, follow-up).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from jobpilot.channels.telegram import (
    IntentKind,
    TelegramDeliveryError,
    TelegramIntent,
    language_buttons,
    parse_intent,
    proposal_buttons,
    render_language_prompt,
    render_proposal_text,
)
from jobpilot.config import AppConfig, Language, Settings, get_settings
from jobpilot.documents.cover import CoverLetterError, generate_cover_letter_body
from jobpilot.documents.render import (
    CoverLetterTooLongError,
    CvTooLongError,
    DocumentContentError,
    render_cover_letter,
    render_cv,
)
from jobpilot.documents.selector import SelectionError, select_content
from jobpilot.filters.prefilter import prefilter
from jobpilot.language import detect_language
from jobpilot.logging import get_logger
from jobpilot.models import Job, JobStatus
from jobpilot.scoring.scorer import analyze_and_score, passes_threshold

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from jobpilot.interfaces import FileStorage, JobRepository, JobSource, LlmClient, Notifier
    from jobpilot.models import Application, Attachment, ContentSelection, RawJob, StoredFile
    from jobpilot.profile.model import ProfileModel

log = get_logger(__name__)

# ``telegram_state`` key holding the id of the last Telegram update processed, so
# each update is acted on exactly once across runs (idempotency).
LAST_UPDATE_ID_KEY = "telegram_last_update_id"
# ``telegram_state`` key holding the job id awaiting a free-text Ajustar instruction.
PENDING_ADJUST_KEY = "telegram_pending_adjust_job_id"

# Spanish labels for every job status, shown in Telegram responses (R16.2). The
# internal ``JobStatus`` values stay English; only these labels reach the owner.
_STATUS_LABELS: dict[JobStatus, str] = {
    JobStatus.new: "nuevas",
    JobStatus.filtered_out: "descartadas por filtro",
    JobStatus.scored: "evaluadas",
    JobStatus.proposed: "propuestas",
    JobStatus.approved: "aprobadas",
    JobStatus.applied: "aplicadas",
    JobStatus.discarded: "descartadas",
    JobStatus.error: "con error",
}


@dataclass(slots=True)
class CollectSummary:
    """Counts returned by :func:`collect_and_persist`."""

    fetched: int = 0
    persisted: int = 0
    filtered_out: int = 0
    kept_new: int = 0
    source_errors: dict[str, str] = field(default_factory=dict)


def collect_and_persist(
    sources: Sequence[JobSource],
    repo: JobRepository,
    config: AppConfig,
) -> CollectSummary:
    """Collect from every source, persist idempotently and prefilter."""
    summary = CollectSummary()
    for source in sources:
        name = getattr(source, "name", source.__class__.__name__)
        try:
            raw_jobs = list(source.fetch())
        except Exception as exc:  # noqa: BLE001 - one source must not stop the rest
            log.error("source_fetch_error", source=name, error=str(exc))
            summary.source_errors[name] = str(exc)
            continue

        for raw in raw_jobs:
            summary.fetched += 1
            job = repo.upsert_job(raw)
            summary.persisted += 1
            if job.id is None:  # pragma: no cover - repositories always assign ids
                log.warning("persisted_job_without_id", source=name)
                continue

            # Only (re)evaluate jobs that are still fresh; never override a job that
            # has already progressed beyond 'new' on a later run (idempotency).
            if job.status != JobStatus.new:
                continue

            if prefilter(raw, config.prefilter, language=raw.language):
                summary.kept_new += 1
            else:
                repo.set_status(job.id, JobStatus.filtered_out)
                summary.filtered_out += 1

    log.info(
        "collect_done",
        fetched=summary.fetched,
        filtered_out=summary.filtered_out,
        kept_new=summary.kept_new,
        source_errors=list(summary.source_errors),
    )
    return summary


def _offer_text(job: Job) -> str:
    """Build the text used for language detection: title plus description."""
    return f"{job.title}\n{job.description}".strip()


@dataclass(slots=True)
class ScoreSummary:
    """Counts returned by :func:`score_new_jobs`."""

    analyzed: int = 0
    scored: int = 0
    below_threshold: int = 0
    errors: int = 0


def score_new_jobs(
    jobs: Sequence[Job],
    repo: JobRepository,
    profile: ProfileModel,
    llm: LlmClient,
    config: AppConfig,
    settings: Settings | None = None,
) -> ScoreSummary:
    """Analyse and score each ``new`` job, persisting analysis, score and status.

    Before scoring, the offer language is resolved with
    :func:`jobpilot.language.detect_language` (lingua, with the LLM confirmation
    step on low-confidence offers), so the deterministic detector and the
    configured ``language.min_confidence`` threshold drive the persisted language.
    A ``None`` result is persisted as the unresolved state (the language is cleared,
    even when the source declared one), leaving the job ambiguous for the later
    Telegram Español/Inglés/Descartar choice flow rather than silently committing to
    a possibly wrong source-declared language.

    A job that scores at or above ``MIN_SCORE`` becomes ``scored`` and is eligible
    for a proposal; one below the threshold is persisted with its analysis/score but
    moved to ``filtered_out`` (a non-continuing state), so a low-scoring job never
    reaches document generation or a Telegram proposal (R8.6/R10.7). An unrecoverable
    LLM/validation failure marks the job ``error``. Jobs not in ``new`` are skipped
    so reruns are idempotent.
    """
    active_settings = settings or get_settings()
    summary = ScoreSummary()
    for job in jobs:
        if job.id is None or job.status != JobStatus.new:
            continue
        try:
            analysis, score = analyze_and_score(job, profile, llm, config)
        except Exception as exc:  # noqa: BLE001 - mark job error, continue with the rest
            log.error("scoring_failed", job_id=job.id, error=str(exc))
            repo.set_status(job.id, JobStatus.error)
            summary.errors += 1
            continue

        summary.analyzed += 1
        # The detector (lingua + optional LLM confirmation) is authoritative here.
        # A None result means the language is unresolved; persist it as None so the
        # offer reaches the Telegram choice flow, instead of restoring a possibly
        # wrong source-declared language.
        detected = detect_language(_offer_text(job), config.language, llm)
        above = passes_threshold(score.score, active_settings.min_score)
        # Below-threshold jobs keep their analysis/score for the record but are moved
        # to filtered_out so they never continue to a proposal (R8.6/R10.7); only
        # jobs at or above MIN_SCORE become 'scored' and proposable.
        status = JobStatus.scored if above else JobStatus.filtered_out
        updated = job.model_copy(
            update={
                "analysis": analysis,
                "language": detected,
                "score": score.score,
                "score_reason": "; ".join(score.reasons) or None,
                "missing_requirements": score.missing_requirements,
                "status": status,
            }
        )
        repo.save_job(updated)
        if above:
            summary.scored += 1
        else:
            summary.below_threshold += 1
            log.info("job_below_threshold", job_id=job.id, score=score.score)

    log.info(
        "score_done",
        analyzed=summary.analyzed,
        scored=summary.scored,
        below_threshold=summary.below_threshold,
        errors=summary.errors,
    )
    return summary


@dataclass(slots=True)
class GeneratedDocuments:
    """The tailored documents produced for a job and their stored links."""

    selection: ContentSelection
    cv: Attachment
    cover_letter: Attachment
    cv_file: StoredFile
    cover_letter_file: StoredFile


def _upload_attachment(storage: FileStorage, attachment: Attachment, tmp_dir: Path) -> StoredFile:
    """Write an attachment to a temp file and upload it, returning its stored link."""
    tmp_path = tmp_dir / attachment.filename
    tmp_path.write_bytes(attachment.content)
    return storage.upload(tmp_path, attachment.filename)


def generate_documents(
    job: Job,
    profile: ProfileModel,
    llm: LlmClient,
    storage: FileStorage,
    *,
    language: Language,
    instruction: str | None = None,
) -> GeneratedDocuments:
    """Select content, compose + render the CV + cover letter, and upload both.

    The LLM only selects which existing profile ids to use for the CV (validated in
    code) and composes the cover-letter body from approved facts only (validated in
    code: <= 200 words, profile-only technologies, no instruction echo). An optional
    ``instruction`` (the owner's free-text Ajustar note) guides BOTH the CV
    re-selection and the cover-letter wording, so a tone/emphasis request such as
    "hazla más formal" changes the actual letter while the instruction text never
    appears in a document. The rendered PDFs are uploaded through the swappable
    :class:`FileStorage`, and the returned ``web_view_link`` values are what a later
    phase persists on the application. Raises on invalid selection or a
    non-compliant cover letter so the caller can mark the job ``error`` rather than
    sending an invented document.
    """
    import tempfile  # noqa: PLC0415 - only needed when documents are generated

    if job.analysis is None:
        raise ValueError("generate_documents requires an analysed job")

    selection = select_content(job.analysis, profile, llm, language, instruction=instruction)
    cover_letter_body = generate_cover_letter_body(
        profile,
        job.analysis,
        selection,
        llm,
        language=language,
        company=job.company,
        instruction=instruction,
    )
    cv = render_cv(profile, selection, company=job.company, role=job.title)
    cover = render_cover_letter(
        profile,
        language=language,
        body=cover_letter_body,
        company=job.company,
        role=job.title,
    )

    with tempfile.TemporaryDirectory(prefix="jobpilot-docs-") as tmp:
        tmp_dir = Path(tmp)
        cv_file = _upload_attachment(storage, cv, tmp_dir)
        cover_file = _upload_attachment(storage, cover, tmp_dir)

    log.info(
        "documents_generated",
        job_id=job.id,
        language=language.value,
        cv_link=cv_file.web_view_link,
        cover_link=cover_file.web_view_link,
    )
    return GeneratedDocuments(
        selection=selection,
        cv=cv,
        cover_letter=cover,
        cv_file=cv_file,
        cover_letter_file=cover_file,
    )


# --------------------------------------------------------------------------- #
# Phase 5 — Telegram orchestration, proposal guards and approval handling
# --------------------------------------------------------------------------- #
class ProposalRepository(Protocol):
    """The repository surface the proposal/approval flow needs.

    Extends the base :class:`~jobpilot.interfaces.JobRepository` contract with the
    two read helpers the guards rely on (implemented by the concrete
    ``NeonJobRepository`` and by the in-memory test fake). Keeping them here, rather
    than widening the minimal Protocol, lets the guards stay type-checked without
    forcing every ``JobRepository`` to grow query methods it does not need.
    """

    def get_job(self, job_id: int) -> Job | None: ...

    def set_status(self, job_id: int, status: JobStatus) -> None: ...

    def save_job(self, job: Job) -> Job: ...

    def get_state(self, key: str) -> str | None: ...

    def set_state(self, key: str, value: str) -> None: ...

    def count_applications_on(self, day: date) -> int: ...

    def recent_company_role(self, company: str, role: str, *, within_days: int) -> bool: ...


def tailor_and_send_proposal(
    job: Job,
    repo: ProposalRepository,
    notifier: Notifier,
    profile: ProfileModel,
    llm: LlmClient,
    storage: FileStorage,
    *,
    language: Language,
    instruction: str | None = None,
) -> GeneratedDocuments:
    """Generate the tailored documents for ``job``, deliver them, then mark proposed.

    Used both when proposing a job whose language is already known and when the
    owner later picks a language or asks to adjust (``instruction`` carries the
    free-text Ajustar note, which guides re-selection but never appears in a
    document). Delivery happens BEFORE the status transition: the documents and the
    proposal message are sent first, and only once Telegram confirms delivery is the
    job persisted as ``proposed``. A delivery rejection raises
    :class:`~jobpilot.channels.telegram.TelegramDeliveryError` and leaves the job in
    its prior state so a later run retries it instead of silently skipping it.
    Nothing moves to ``approved``/``applied`` without a later explicit button.
    """
    job_id = job.id
    if job_id is None:
        raise ValueError("tailor_and_send_proposal requires a persisted job with an id")
    documents = generate_documents(
        job,
        profile,
        llm,
        storage,
        language=language,
        instruction=instruction,
    )
    # Persist the resolved language on the job we render the proposal text from, but
    # only commit the 'proposed' status AFTER delivery is confirmed below.
    proposed = job.model_copy(update={"language": language, "status": JobStatus.proposed})
    notifier.send_message(
        render_proposal_text(proposed),
        buttons=proposal_buttons(job_id),
        attachments=[documents.cv, documents.cover_letter],
    )
    repo.save_job(proposed)
    log.info("proposal_sent", job_id=job_id, language=language.value)
    return documents


@dataclass(slots=True)
class ProposeSummary:
    """Counts returned by :func:`propose_scored_jobs`."""

    proposed: int = 0
    language_prompts: int = 0
    skipped_daily_limit: int = 0
    skipped_duplicate: int = 0


def propose_scored_jobs(
    jobs: Sequence[Job],
    repo: ProposalRepository,
    notifier: Notifier,
    profile: ProfileModel,
    llm: LlmClient,
    storage: FileStorage,
    config: AppConfig,
    settings: Settings | None = None,
    *,
    today: date | None = None,
) -> ProposeSummary:
    """Propose ``scored`` jobs through Telegram, enforcing the proposal guards.

    For each job still in ``scored``:

    * the daily guard stops once the number of applications already made today plus
      the number proposed in this run reaches ``DAILY_APPLICATION_LIMIT``;
    * the company+role guard skips a job already seen (proposed or applied) within
      ``proposals.dedupe_company_role_days`` and marks it ``discarded``;
    * a job whose language is still undecided gets the Español/Inglés/Descartar
      prompt and stays ``proposed`` awaiting the choice (no documents yet);
    * otherwise the tailored documents are generated and the full proposal is sent,
      leaving the job ``proposed`` awaiting an explicit approval button.
    """
    active_settings = settings or get_settings()
    day = today or date.today()
    limit = active_settings.daily_application_limit
    dedupe_days = config.proposals.dedupe_company_role_days
    already_today = repo.count_applications_on(day)
    summary = ProposeSummary()

    for job in jobs:
        if job.id is None or job.status != JobStatus.scored:
            continue
        if already_today + summary.proposed >= limit:
            summary.skipped_daily_limit += 1
            log.info("proposal_skipped_daily_limit", job_id=job.id, limit=limit)
            continue
        if repo.recent_company_role(job.company, job.title, within_days=dedupe_days):
            repo.set_status(job.id, JobStatus.discarded)
            summary.skipped_duplicate += 1
            log.info("proposal_skipped_duplicate", job_id=job.id, company=job.company)
            continue

        job_id = job.id
        # ``partial`` binds this job eagerly, so the per-job actions below are not
        # affected by the loop variable advancing on the next iteration.
        mark_error = partial(repo.set_status, job_id, JobStatus.error)
        if job.language is None:
            # Send the Español/Inglés/Descartar prompt FIRST; only mark the job
            # proposed (awaiting the choice) once the prompt was delivered, so a
            # rejected prompt leaves the job 'scored' to retry rather than stranding
            # it as proposed with no message sent. Any failure is isolated per job
            # (delivery/transport retryable; a selection/content error marks error).
            if _try_proposal(
                job_id,
                partial(_ask_language_and_mark, repo, notifier, job),
                on_unrecoverable=mark_error,
            ):
                summary.language_prompts += 1
                summary.proposed += 1
            continue

        # Generate + deliver + persist. A selection/validation/content failure is
        # unrecoverable and marks the job 'error'; a delivery, storage or render
        # failure is retryable and leaves the job 'scored'. Either way the batch
        # continues with the rest, and later Telegram update processing still runs.
        if _try_proposal(
            job_id,
            partial(
                tailor_and_send_proposal,
                job,
                repo,
                notifier,
                profile,
                llm,
                storage,
                language=job.language,
            ),
            on_unrecoverable=mark_error,
        ):
            summary.proposed += 1

    log.info(
        "propose_done",
        proposed=summary.proposed,
        language_prompts=summary.language_prompts,
        skipped_daily_limit=summary.skipped_daily_limit,
        skipped_duplicate=summary.skipped_duplicate,
    )
    return summary


# Failures that mean the selection can never produce a valid document; the job is
# marked ``error`` and never retried. A delivery failure is handled separately as a
# retryable condition (the job keeps its prior state).
_UNRECOVERABLE_DOCUMENT_ERRORS = (
    SelectionError,
    CoverLetterError,
    CoverLetterTooLongError,
    CvTooLongError,
    DocumentContentError,
)


def _ask_language_and_mark(repo: ProposalRepository, notifier: Notifier, job: Job) -> None:
    """Send the ambiguous-language prompt, then mark the job proposed (unresolved).

    The prompt is sent first so a delivery rejection propagates before the status
    changes, leaving the job ``scored`` to retry instead of proposed with nothing
    sent. The persisted job keeps ``language = None`` to mark the unresolved state.
    """
    if job.id is None:  # pragma: no cover - callers guarantee an id
        raise ValueError("_ask_language_and_mark requires a persisted job with an id")
    notifier.send_message(render_language_prompt(job), buttons=language_buttons(job.id))
    repo.save_job(job.model_copy(update={"status": JobStatus.proposed, "language": None}))


def _try_proposal(
    job_id: int,
    action: Callable[[], object],
    *,
    on_unrecoverable: Callable[[], object] | None = None,
) -> bool:
    """Run a proposal ``action`` for one job, isolating its failure from the batch.

    Returns ``True`` when the action completed. Three failure classes are handled so
    one bad job never aborts the rest of the batch (or the later Telegram update
    processing):

    * an unrecoverable selection/validation/content failure runs
      ``on_unrecoverable`` (used to mark the job ``error``) — the selection can never
      produce a valid document, so retrying is pointless;
    * a retryable Telegram delivery failure is logged and the job keeps its prior
      state for a later run;
    * any OTHER failure (a Drive/storage API error, a transient WeasyPrint render
      failure, an unexpected transport error) is treated as retryable too: it is
      logged and the job keeps its prior state, so a single transient upload/render
      error does not stop later proposals or queued approvals.
    """
    try:
        action()
    except _UNRECOVERABLE_DOCUMENT_ERRORS as exc:
        log.error("proposal_generation_failed", job_id=job_id, error=str(exc))
        if on_unrecoverable is not None:
            on_unrecoverable()
        return False
    except TelegramDeliveryError as exc:
        log.warning("proposal_delivery_failed", job_id=job_id, error=str(exc))
        return False
    except Exception as exc:  # noqa: BLE001 - keep prior state, isolate from the batch
        log.warning("proposal_retryable_failure", job_id=job_id, error=str(exc))
        return False
    return True


@dataclass(slots=True)
class TelegramRunSummary:
    """Counts returned by :func:`process_telegram_updates`."""

    processed: int = 0
    approved: int = 0
    discarded: int = 0
    applied: int = 0
    regenerated: int = 0
    commands: int = 0
    ignored: int = 0


def process_telegram_updates(
    repo: ProposalRepository,
    notifier: Notifier,
    profile: ProfileModel,
    llm: LlmClient,
    storage: FileStorage,
    config: AppConfig,
) -> TelegramRunSummary:
    """Read and act on Telegram updates exactly once each (human-in-the-loop).

    The ``last_update_id`` persisted in ``telegram_state`` is used both as the
    ``getUpdates`` offset and as an in-code guard, so an update is never acted on
    twice even if Telegram returns it again. The handlers themselves are idempotent:
    every status-changing action requires the job to be in an eligible state, so a
    replayed update (for example after a crash between the effect and the checkpoint)
    finds the job already in its new state and makes no further change.

    The checkpoint is advanced to the current update's id BEFORE its handler runs
    (a durable claim), so every update's external effects — document uploads and
    Telegram sends included — happen at most once. This is what makes the free-text
    Ajustar path idempotent: it changes documents and sends messages without
    changing the job status, so re-running it would NOT be a no-op; claiming the
    update first guarantees a crash after delivery never replays that adjustment.
    Status-changing actions remain idempotent on their own via the eligible-state
    guard, so a Telegram-side replay before the claim persisted is still safe.

    After the batch, the cursor is advanced to the highest *raw* update id Telegram
    returned — including ignored non-owner updates — so a batch made only of
    unauthorized messages still moves forward and can never starve a later owner
    update.
    """
    summary = TelegramRunSummary()
    last_raw = repo.get_state(LAST_UPDATE_ID_KEY)
    last_update_id = int(last_raw) if last_raw is not None else None
    offset = last_update_id + 1 if last_update_id is not None else None

    batch = notifier.get_update_batch(offset=offset)
    updates = sorted(batch.updates, key=lambda u: u.update_id)
    for update in updates:
        if last_update_id is not None and update.update_id <= last_update_id:
            continue
        # Claim the update durably BEFORE running its effects: advancing the cursor
        # first makes each update_id processed at most once, so a crash after a
        # delivery (notably a free-text adjustment, which does not change status)
        # never regenerates, re-uploads, or re-sends on the next run.
        last_update_id = update.update_id
        repo.set_state(LAST_UPDATE_ID_KEY, str(last_update_id))
        intent = parse_intent(update)
        _handle_intent(intent, repo, notifier, profile, llm, storage, config, summary)
        summary.processed += 1

    # Advance past ignored non-owner updates so they are never re-fetched forever.
    highest = batch.highest_update_id
    if highest is not None and (last_update_id is None or highest > last_update_id):
        repo.set_state(LAST_UPDATE_ID_KEY, str(highest))

    log.info(
        "telegram_run_done",
        processed=summary.processed,
        approved=summary.approved,
        discarded=summary.discarded,
        applied=summary.applied,
        regenerated=summary.regenerated,
        commands=summary.commands,
        ignored=summary.ignored,
    )
    return summary


def _handle_intent(  # noqa: C901, PLR0912 - a flat dispatch over the intent kinds
    intent: TelegramIntent,
    repo: ProposalRepository,
    notifier: Notifier,
    profile: ProfileModel,
    llm: LlmClient,
    storage: FileStorage,
    config: AppConfig,
    summary: TelegramRunSummary,
) -> None:
    kind = intent.kind
    if kind is IntentKind.send:
        if _transition(repo, notifier, intent.job_id, JobStatus.approved, "Postulación aprobada."):
            summary.approved += 1
    elif kind is IntentKind.discard:
        if _transition(repo, notifier, intent.job_id, JobStatus.discarded, "Oferta descartada."):
            summary.discarded += 1
    elif kind is IntentKind.mark_applied:
        if _transition(repo, notifier, intent.job_id, JobStatus.applied, "Marcada como aplicada."):
            summary.applied += 1
    elif kind is IntentKind.choose_language:
        if _handle_language_choice(intent, repo, notifier, profile, llm, storage):
            summary.regenerated += 1
    elif kind is IntentKind.adjust:
        _handle_adjust_request(intent, repo, notifier)
        summary.commands += 1
    elif kind is IntentKind.free_text:
        _handle_free_text(intent, repo, notifier, profile, llm, storage, summary)
    elif kind in _COMMAND_KINDS:
        _handle_command(intent, repo, notifier, config)
        summary.commands += 1
    else:
        summary.ignored += 1
        notifier.send_message("No entendí esa acción. Usa los botones o un comando válido.")


_COMMAND_KINDS = frozenset(
    {
        IntentKind.list_pending,
        IntentKind.status,
        IntentKind.mark_interview,
        IntentKind.mark_rejected,
        IntentKind.search,
    }
)


# The job statuses from which each owner action is allowed. Enforcing these makes
# the callbacks idempotent and rejects stale/crafted clicks: a second 'Enviar' on an
# already-approved job, or an approval of a 'new'/'scored'/'discarded' job, changes
# nothing. 'Ya apliqué' is valid both straight from a proposal and after an approval
# (the manual send path).
_ELIGIBLE_SOURCES: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.approved: frozenset({JobStatus.proposed}),
    JobStatus.discarded: frozenset({JobStatus.proposed}),
    JobStatus.applied: frozenset({JobStatus.proposed, JobStatus.approved}),
}


def _transition(
    repo: ProposalRepository,
    notifier: Notifier,
    job_id: int | None,
    status: JobStatus,
    message: str,
) -> bool:
    """Apply a status transition only if the job is in an eligible source state.

    Returns ``True`` when the transition was applied. A missing job, or one whose
    current status is not eligible for ``status`` (a stale or out-of-order
    callback), is rejected without any data change, so finished jobs are never
    resurrected and ineligible jobs are never approved.
    """
    if job_id is None:
        notifier.send_message("No pude identificar la oferta de esa acción.")
        return False
    job = repo.get_job(job_id)
    if job is None:
        notifier.send_message(f"No encontré la oferta {job_id}.")
        return False
    if job.status not in _ELIGIBLE_SOURCES.get(status, frozenset()):
        log.info(
            "telegram_stale_action_ignored",
            job_id=job_id,
            current=job.status.value,
            requested=status.value,
        )
        notifier.send_message(
            f"Esa acción ya no aplica para la oferta {job_id} "
            f"(estado actual: {_STATUS_LABELS[job.status]})."
        )
        return False
    repo.set_status(job_id, status)
    notifier.send_message(f"{message} ({job.company} — {job.title})")
    return True


def _handle_language_choice(
    intent: TelegramIntent,
    repo: ProposalRepository,
    notifier: Notifier,
    profile: ProfileModel,
    llm: LlmClient,
    storage: FileStorage,
) -> bool:
    """Resolve the language for a proposed-but-undecided job and send the proposal.

    Only valid when the job is ``proposed`` with an unresolved language; a choice for
    any other job (already resolved, discarded, not yet proposed) is a stale callback
    and is rejected without change — which also makes a replay idempotent, since the
    first success leaves the job with a language set. Returns ``True`` only when a
    regeneration actually ran and was delivered.
    """
    if intent.job_id is None or intent.language is None:
        notifier.send_message("No pude procesar la elección de idioma.")
        return False
    job = repo.get_job(intent.job_id)
    if job is None:
        notifier.send_message(f"No encontré la oferta {intent.job_id}.")
        return False
    if job.status is not JobStatus.proposed or job.language is not None:
        log.info(
            "telegram_stale_language_choice_ignored",
            job_id=intent.job_id,
            current=job.status.value,
            language=job.language.value if job.language else None,
        )
        notifier.send_message(
            f"La oferta {intent.job_id} ya no está esperando una elección de idioma."
        )
        return False
    job_id = intent.job_id
    language = intent.language
    return _try_proposal(
        job_id,
        lambda: tailor_and_send_proposal(
            job, repo, notifier, profile, llm, storage, language=language
        ),
        on_unrecoverable=lambda: repo.set_status(job_id, JobStatus.error),
    )


def _handle_adjust_request(
    intent: TelegramIntent,
    repo: ProposalRepository,
    notifier: Notifier,
) -> None:
    """Arm a pending free-text adjustment, but only for a valid target job.

    Like the other callbacks, this validates the job state before mutating any
    state: only an existing, language-resolved ``proposed`` job can be adjusted.
    A stale or crafted ``adjust:<id>`` for an applied, discarded, scored, missing,
    or unresolved-language job is rejected WITHOUT touching ``PENDING_ADJUST_KEY``,
    so it can never overwrite a valid pending adjustment or arm an adjustment for an
    ineligible job.
    """
    if intent.job_id is None:
        notifier.send_message("No pude identificar la oferta a ajustar.")
        return
    job = repo.get_job(intent.job_id)
    if job is None:
        notifier.send_message(f"No encontré la oferta {intent.job_id}.")
        return
    if job.status is not JobStatus.proposed or job.language is None:
        log.info(
            "telegram_stale_adjust_ignored",
            job_id=intent.job_id,
            current=job.status.value,
            language=job.language.value if job.language else None,
        )
        notifier.send_message(
            f"La oferta {intent.job_id} no está lista para ajustes "
            f"(estado actual: {_STATUS_LABELS[job.status]})."
        )
        return
    repo.set_state(PENDING_ADJUST_KEY, str(intent.job_id))
    notifier.send_message(
        "Envíame en un mensaje la instrucción para ajustar los documentos de esta oferta."
    )


def _pending_adjust_job_id(repo: ProposalRepository) -> int | None:
    """Return the job id awaiting an Ajustar instruction, or ``None`` if none.

    The marker is cleared to an empty string after a completed or failed
    adjustment, so an empty or otherwise non-numeric value is treated as "no pending
    adjustment" rather than being fed to ``int()`` (which would abort the batch and
    leave the update uncheckpointed to fail forever).
    """
    raw = repo.get_state(PENDING_ADJUST_KEY)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("telegram_invalid_pending_adjust", value=raw)
        return None


def _handle_free_text(
    intent: TelegramIntent,
    repo: ProposalRepository,
    notifier: Notifier,
    profile: ProfileModel,
    llm: LlmClient,
    storage: FileStorage,
    summary: TelegramRunSummary,
) -> None:
    pending_id = _pending_adjust_job_id(repo)
    if pending_id is None:
        summary.ignored += 1
        notifier.send_message("No hay ninguna oferta esperando ajustes. Usa los botones.")
        return
    job = repo.get_job(pending_id)
    if job is None or job.language is None or job.status is not JobStatus.proposed:
        repo.set_state(PENDING_ADJUST_KEY, "")
        notifier.send_message("No pude ajustar: la oferta no está lista o no tiene idioma.")
        return
    language = job.language
    ran = _try_proposal(
        pending_id,
        lambda: tailor_and_send_proposal(
            job, repo, notifier, profile, llm, storage, language=language, instruction=intent.text
        ),
        on_unrecoverable=lambda: repo.set_status(pending_id, JobStatus.error),
    )
    # Clear the marker whether the regeneration succeeded or failed unrecoverably, so
    # the next free-text message is not mistaken for another adjustment. A retryable
    # delivery failure also clears it (the owner can press Ajustar again).
    repo.set_state(PENDING_ADJUST_KEY, "")
    if ran:
        summary.regenerated += 1


def _handle_command(
    intent: TelegramIntent,
    repo: ProposalRepository,
    notifier: Notifier,
    config: AppConfig,
) -> None:
    kind = intent.kind
    if kind is IntentKind.list_pending:
        notifier.send_message(_render_pending(repo))
    elif kind is IntentKind.status:
        notifier.send_message(_render_status(repo))
    elif kind is IntentKind.mark_interview:
        _record_outcome(repo, notifier, intent.job_id, "entrevista")
    elif kind is IntentKind.mark_rejected:
        _record_outcome(repo, notifier, intent.job_id, "rechazo")
    elif kind is IntentKind.search:
        query = intent.text or "(sin filtro)"
        notifier.send_message(f"Búsqueda solicitada: {query}. Se ejecutará en la próxima corrida.")


def _render_pending(repo: ProposalRepository) -> str:
    jobs = _proposed_jobs(repo)
    if not jobs:
        return "No hay ofertas pendientes de decisión."
    lines = ["Ofertas pendientes:"]
    lines.extend(f"- {job.id}: {job.company} — {job.title}" for job in jobs)
    return "\n".join(lines)


def _render_status(repo: ProposalRepository) -> str:
    counts: dict[JobStatus, int] = {}
    for job in _all_jobs(repo):
        counts[job.status] = counts.get(job.status, 0) + 1
    if not counts:
        return "Aún no hay ofertas registradas."
    lines = ["Estado de las ofertas:"]
    # Report in a stable order using the Spanish labels (R16.2: Telegram copy is
    # Spanish, so the internal English status values are never shown to the owner).
    lines.extend(
        f"- {_STATUS_LABELS[status]}: {counts[status]}" for status in JobStatus if status in counts
    )
    return "\n".join(lines)


def _record_outcome(
    repo: ProposalRepository,
    notifier: Notifier,
    job_id: int | None,
    label: str,
) -> None:
    # Outcome persistence on the application is completed in Phase 7; here we only
    # acknowledge the owner's command so the Telegram contract is testable.
    if job_id is None:
        notifier.send_message(f"Usa el formato: /{label} <id>.")
        return
    job = repo.get_job(job_id)
    if job is None:
        notifier.send_message(f"No encontré la oferta {job_id}.")
        return
    notifier.send_message(f"Registrado {label} para {job.company} — {job.title}.")


def _proposed_jobs(repo: ProposalRepository) -> list[Job]:
    lister = getattr(repo, "list_proposed", None)
    if callable(lister):
        result: list[Job] = list(lister())
        return result
    return [job for job in _all_jobs(repo) if job.status is JobStatus.proposed]


def _all_jobs(repo: ProposalRepository) -> list[Job]:
    jobs = getattr(repo, "jobs", None)
    if isinstance(jobs, list):
        return [job for job in jobs if isinstance(job, Job)]
    return []


# --------------------------------------------------------------------------- #
# One-run orchestration (wired to ``jobpilot run``)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RunSummary:
    """The end-to-end summary of a single pipeline run."""

    collect: CollectSummary
    score: ScoreSummary
    propose: ProposeSummary
    telegram: TelegramRunSummary


class RunRepository(Protocol):
    """The repository surface a full run needs.

    A superset of :class:`~jobpilot.interfaces.JobRepository` (so it can drive
    collection and scoring) plus the proposal-guard and status-listing helpers the
    proposal/approval stages use. Satisfied by the concrete ``NeonJobRepository``
    and by the in-memory test fake.
    """

    def upsert_job(self, job: RawJob) -> Job: ...

    def get_job(self, job_id: int) -> Job | None: ...

    def set_status(self, job_id: int, status: JobStatus) -> None: ...

    def save_job(self, job: Job) -> Job: ...

    def create_application(self, app: Application) -> Application: ...

    def get_state(self, key: str) -> str | None: ...

    def set_state(self, key: str, value: str) -> None: ...

    def count_applications_on(self, day: date) -> int: ...

    def recent_company_role(self, company: str, role: str, *, within_days: int) -> bool: ...

    def list_by_status(self, status: JobStatus) -> list[Job]: ...


def run_once(
    sources: Sequence[JobSource],
    repo: RunRepository,
    notifier: Notifier,
    profile: ProfileModel,
    llm: LlmClient,
    storage: FileStorage,
    config: AppConfig,
    settings: Settings | None = None,
) -> RunSummary:
    """Run the whole pipeline once: collect, score, propose, process approvals.

    This is the single production orchestrator behind ``jobpilot run``. It composes
    the already-tested stages end to end — collection and dedupe, analysis/scoring
    with the ``MIN_SCORE`` gate, proposal generation under the daily and
    company+role guards, and the human-in-the-loop Telegram update processing — so a
    user or a scheduled invocation reaches proposal creation and approval handling.
    Dependencies are injected so the composition root (the CLI) owns construction of
    the real Neon/Telegram/Drive/LLM implementations and tests can pass fakes.
    """
    active_settings = settings or get_settings()
    collect = collect_and_persist(sources, repo, config)
    score = score_new_jobs(
        repo.list_by_status(JobStatus.new), repo, profile, llm, config, active_settings
    )
    propose = propose_scored_jobs(
        repo.list_by_status(JobStatus.scored),
        repo,
        notifier,
        profile,
        llm,
        storage,
        config,
        active_settings,
    )
    telegram = process_telegram_updates(repo, notifier, profile, llm, storage, config)
    log.info("run_once_done")
    return RunSummary(collect=collect, score=score, propose=propose, telegram=telegram)
