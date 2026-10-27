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
from pathlib import Path
from typing import TYPE_CHECKING

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

    from jobpilot.interfaces import FileStorage, JobRepository, JobSource, LlmClient
    from jobpilot.models import Attachment, ContentSelection, StoredFile
    from jobpilot.profile.model import ProfileModel

log = get_logger(__name__)


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
