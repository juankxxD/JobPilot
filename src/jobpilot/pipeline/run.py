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
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from jobpilot.channels.telegram import (
    IntentKind,
    TelegramIntent,
    language_buttons,
    parse_intent,
    proposal_buttons,
    render_language_prompt,
    render_proposal_text,
)
from jobpilot.config import AppConfig, Language, Settings, get_settings
from jobpilot.documents.render import (
    MAX_COVER_WORDS,
    CoverLetterTooLongError,
    count_words,
    render_cover_letter,
    render_cv,
)
from jobpilot.documents.selector import select_content
from jobpilot.filters.prefilter import prefilter
from jobpilot.language import detect_language
from jobpilot.logging import get_logger
from jobpilot.models import Job, JobStatus
from jobpilot.scoring.scorer import analyze_and_score, passes_threshold

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jobpilot.interfaces import FileStorage, JobRepository, JobSource, LlmClient, Notifier
    from jobpilot.models import Attachment, ContentSelection, StoredFile
    from jobpilot.profile.model import ProfileModel

log = get_logger(__name__)

# ``telegram_state`` key holding the id of the last Telegram update processed, so
# each update is acted on exactly once across runs (idempotency).
LAST_UPDATE_ID_KEY = "telegram_last_update_id"
# ``telegram_state`` key holding the job id awaiting a free-text Ajustar instruction.
PENDING_ADJUST_KEY = "telegram_pending_adjust_job_id"


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

    A job that scores at or above ``MIN_SCORE`` becomes ``scored``; one below the
    threshold is still persisted as ``scored`` but flagged (its score is below
    ``MIN_SCORE``). An unrecoverable LLM/validation failure marks the job ``error``.
    Jobs not in ``new`` are skipped so reruns are idempotent.
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
        updated = job.model_copy(
            update={
                "analysis": analysis,
                "language": detected,
                "score": score.score,
                "score_reason": "; ".join(score.reasons) or None,
                "missing_requirements": score.missing_requirements,
                "status": JobStatus.scored,
            }
        )
        repo.save_job(updated)
        summary.scored += 1
        if not above:
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
    cover_letter_body: str,
) -> GeneratedDocuments:
    """Select content, render the CV + cover letter, and upload both.

    The LLM only selects which existing profile ids to use (validated in code); the
    cover-letter body is enforced to be <= 200 words before rendering. The rendered
    PDFs are uploaded through the swappable :class:`FileStorage`, and the returned
    ``web_view_link`` values are what a later phase persists on the application.
    Raises on invalid selection or an over-length cover letter so the caller can
    mark the job ``error`` rather than sending an invented document.
    """
    import tempfile  # noqa: PLC0415 - only needed when documents are generated

    if job.analysis is None:
        raise ValueError("generate_documents requires an analysed job")

    # Enforce the cover-letter word limit before any (WeasyPrint) rendering, so an
    # over-length letter never reaches the PDF step regardless of document order.
    if count_words(cover_letter_body) > MAX_COVER_WORDS:
        raise CoverLetterTooLongError(
            f"cover letter has {count_words(cover_letter_body)} words, "
            f"the limit is {MAX_COVER_WORDS}"
        )

    selection = select_content(job.analysis, profile, llm, language)
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


def _cover_letter_body(
    profile: ProfileModel,
    *,
    language: Language,
    company: str,
    instruction: str | None = None,
) -> str:
    """Compose a deterministic cover-letter body from the profile summary.

    The body is built in code from an existing profile summary variant (never
    invented) and kept within the 200-word limit. An optional Ajustar instruction
    is appended verbatim so a regeneration reflects the owner's note; the result is
    still truncated to the word limit so rendering never rejects it.
    """
    variant = next(iter(profile.summary_variants.values()))
    summary = variant.text(language)
    if language is Language.es:
        greeting = f"Estimado equipo de {company}:"
        closing = f"Quedo atento a sus comentarios.\n\n{profile.basics.name}"
    else:
        greeting = f"Dear {company} team,"
        closing = f"I look forward to hearing from you.\n\n{profile.basics.name}"
    parts = [greeting, summary]
    if instruction:
        parts.append(instruction.strip())
    parts.append(closing)
    body = "\n\n".join(p for p in parts if p)
    words = body.split()
    if len(words) > MAX_COVER_WORDS:
        body = " ".join(words[:MAX_COVER_WORDS])
    return body


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
    """Generate the tailored documents for ``job`` and send the Telegram proposal.

    Used both when proposing a job whose language is already known and when the
    owner later picks a language or asks to adjust (``instruction`` carries the
    free-text Ajustar note). The job's language is persisted and its status is left
    ``proposed`` awaiting an explicit button; nothing is sent without that button.
    """
    job_id = job.id
    if job_id is None:
        raise ValueError("tailor_and_send_proposal requires a persisted job with an id")
    body = _cover_letter_body(
        profile, language=language, company=job.company, instruction=instruction
    )
    documents = generate_documents(
        job, profile, llm, storage, language=language, cover_letter_body=body
    )
    updated = job.model_copy(update={"language": language, "status": JobStatus.proposed})
    repo.save_job(updated)
    notifier.send_message(
        render_proposal_text(updated),
        buttons=proposal_buttons(job_id),
        attachments=[documents.cv, documents.cover_letter],
    )
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

        if job.language is None:
            repo.set_status(job.id, JobStatus.proposed)
            _ask_language(notifier, job)
            summary.language_prompts += 1
            summary.proposed += 1
            continue

        tailor_and_send_proposal(job, repo, notifier, profile, llm, storage, language=job.language)
        summary.proposed += 1

    log.info(
        "propose_done",
        proposed=summary.proposed,
        language_prompts=summary.language_prompts,
        skipped_daily_limit=summary.skipped_daily_limit,
        skipped_duplicate=summary.skipped_duplicate,
    )
    return summary


def _ask_language(notifier: Notifier, job: Job) -> None:
    if job.id is None:  # pragma: no cover - callers guarantee an id
        raise ValueError("_ask_language requires a persisted job with an id")
    notifier.send_message(render_language_prompt(job), buttons=language_buttons(job.id))


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
    twice even if Telegram returns it again. Each handled update advances
    ``last_update_id`` before the next, so a crash mid-batch never reprocesses what
    already ran. Only the configured owner's updates reach here (the notifier drops
    the rest).
    """
    summary = TelegramRunSummary()
    last_raw = repo.get_state(LAST_UPDATE_ID_KEY)
    last_update_id = int(last_raw) if last_raw is not None else None
    offset = last_update_id + 1 if last_update_id is not None else None

    updates = sorted(notifier.get_updates(offset=offset), key=lambda u: u.update_id)
    for update in updates:
        if last_update_id is not None and update.update_id <= last_update_id:
            continue
        intent = parse_intent(update)
        _handle_intent(intent, repo, notifier, profile, llm, storage, config, summary)
        last_update_id = update.update_id
        repo.set_state(LAST_UPDATE_ID_KEY, str(last_update_id))
        summary.processed += 1

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
        _transition(repo, notifier, intent.job_id, JobStatus.approved, "Postulación aprobada.")
        summary.approved += 1
    elif kind is IntentKind.discard:
        _transition(repo, notifier, intent.job_id, JobStatus.discarded, "Oferta descartada.")
        summary.discarded += 1
    elif kind is IntentKind.mark_applied:
        _transition(repo, notifier, intent.job_id, JobStatus.applied, "Marcada como aplicada.")
        summary.applied += 1
    elif kind is IntentKind.choose_language:
        _handle_language_choice(intent, repo, notifier, profile, llm, storage)
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


def _transition(
    repo: ProposalRepository,
    notifier: Notifier,
    job_id: int | None,
    status: JobStatus,
    message: str,
) -> None:
    if job_id is None:
        notifier.send_message("No pude identificar la oferta de esa acción.")
        return
    job = repo.get_job(job_id)
    if job is None:
        notifier.send_message(f"No encontré la oferta {job_id}.")
        return
    repo.set_status(job_id, status)
    notifier.send_message(f"{message} ({job.company} — {job.title})")


def _handle_language_choice(
    intent: TelegramIntent,
    repo: ProposalRepository,
    notifier: Notifier,
    profile: ProfileModel,
    llm: LlmClient,
    storage: FileStorage,
) -> None:
    if intent.job_id is None or intent.language is None:
        notifier.send_message("No pude procesar la elección de idioma.")
        return
    job = repo.get_job(intent.job_id)
    if job is None:
        notifier.send_message(f"No encontré la oferta {intent.job_id}.")
        return
    tailor_and_send_proposal(job, repo, notifier, profile, llm, storage, language=intent.language)


def _handle_adjust_request(
    intent: TelegramIntent,
    repo: ProposalRepository,
    notifier: Notifier,
) -> None:
    if intent.job_id is None:
        notifier.send_message("No pude identificar la oferta a ajustar.")
        return
    repo.set_state(PENDING_ADJUST_KEY, str(intent.job_id))
    notifier.send_message(
        "Envíame en un mensaje la instrucción para ajustar los documentos de esta oferta."
    )


def _handle_free_text(
    intent: TelegramIntent,
    repo: ProposalRepository,
    notifier: Notifier,
    profile: ProfileModel,
    llm: LlmClient,
    storage: FileStorage,
    summary: TelegramRunSummary,
) -> None:
    pending = repo.get_state(PENDING_ADJUST_KEY)
    if pending is None:
        summary.ignored += 1
        notifier.send_message("No hay ninguna oferta esperando ajustes. Usa los botones.")
        return
    job = repo.get_job(int(pending))
    if job is None or job.language is None:
        repo.set_state(PENDING_ADJUST_KEY, "")
        notifier.send_message("No pude ajustar: la oferta no está lista o no tiene idioma.")
        return
    tailor_and_send_proposal(
        job,
        repo,
        notifier,
        profile,
        llm,
        storage,
        language=job.language,
        instruction=intent.text,
    )
    repo.set_state(PENDING_ADJUST_KEY, "")
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
    counts: dict[str, int] = {}
    for job in _all_jobs(repo):
        counts[job.status.value] = counts.get(job.status.value, 0) + 1
    if not counts:
        return "Aún no hay ofertas registradas."
    lines = ["Estado de las ofertas:"]
    lines.extend(f"- {status}: {count}" for status, count in sorted(counts.items()))
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
