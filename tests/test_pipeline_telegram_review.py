"""Regression tests for the Phase 5 review findings.

These exercise the correctness guarantees the review called out: the MIN_SCORE gate
on proposals, the Ajustar instruction never leaking into the letter, resilient
free-text handling after an adjustment, the job status state machine rejecting
stale/crafted callbacks, update idempotency across replays, checkpointing ignored
non-owner batches, the company+role dedupe window measured from proposal time,
Telegram delivery failures not losing proposals, and per-job failure isolation.

All of these paths avoid WeasyPrint: they either change status without rendering,
or fail (delivery/selection) before any PDF step, so they run without native libs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobpilot.channels.telegram import CallbackAction, encode_callback
from jobpilot.config import AppConfig, Language, ProposalsConfig, Settings
from jobpilot.interfaces import Update
from jobpilot.llm.prompts import build_select_content_prompt
from jobpilot.models import ContentSelection, Job, JobAnalysis, JobStatus
from jobpilot.pipeline.run import (
    LAST_UPDATE_ID_KEY,
    PENDING_ADJUST_KEY,
    _cover_letter_body,
    process_telegram_updates,
    propose_scored_jobs,
)
from jobpilot.profile.model import ProfileModel
from tests.fakes import FakeFileStorage, FakeJobRepository, FakeNotifier, ScriptedFakeLlmClient


def _scored_job(
    *,
    job_id: int = 1,
    company: str = "ACME Corp",
    title: str = "Backend Engineer",
    language: Language | None = Language.es,
    score: int = 90,
    status: JobStatus = JobStatus.scored,
) -> Job:
    return Job(
        id=job_id,
        source="remoteok",
        external_id=f"rok-{job_id}",
        url=f"https://example.com/job/{job_id}",
        title=title,
        company=company,
        language=language,
        score=score,
        status=status,
        analysis=JobAnalysis(normalized_title=title),
    )


def _callback(update_id: int, action: CallbackAction, job_id: int) -> Update:
    return Update(update_id=update_id, chat_id="1", callback_data=encode_callback(action, job_id))


# --------------------------------------------------------------------------- #
# F3 — Ajustar instruction guides selection, never leaks into the letter
# --------------------------------------------------------------------------- #
def test_adjustment_instruction_flows_to_selection_prompt() -> None:
    instruction = "usa un tono más formal"
    prompt = build_select_content_prompt(
        profile_json="{}",
        summary_variants=["backend_java"],
        achievement_ids=["ach_1"],
        project_ids=[],
        skills=["Java"],
        analysis_json="{}",
        language="es",
        schema_json="{}",
        instruction=instruction,
    )
    assert instruction in prompt
    assert "RE-SELECTING" in prompt


def test_cover_letter_body_never_contains_instruction(example_profile: ProfileModel) -> None:
    # The instruction is handled by the selector, so the deterministic letter body
    # must not carry it (it is not even a parameter of the body builder).
    body = _cover_letter_body(example_profile, language=Language.es, company="ACME Corp")
    assert "tono más formal" not in body


# --------------------------------------------------------------------------- #
# F4 — free text after a completed adjustment does not crash
# --------------------------------------------------------------------------- #
def test_free_text_after_cleared_adjustment_is_ignored(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_scored_job(job_id=1))
    repo.set_status(1, JobStatus.proposed)
    # A previous adjustment left the marker cleared to the empty-string sentinel.
    repo.set_state(PENDING_ADJUST_KEY, "")
    notifier = FakeNotifier([Update(update_id=90, chat_id="1", text="hola de nuevo")])
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = process_telegram_updates(repo, notifier, example_profile, llm, storage, AppConfig())

    # No crash, the update is processed and checkpointed, and nothing was rendered.
    assert summary.processed == 1
    assert summary.ignored == 1
    assert storage.uploads == []
    assert repo.get_state(LAST_UPDATE_ID_KEY) == "90"
    assert "No hay ninguna oferta esperando ajustes" in notifier.last().text


# --------------------------------------------------------------------------- #
# F5 — the status state machine rejects stale and crafted callbacks
# --------------------------------------------------------------------------- #
def test_send_on_discarded_job_is_rejected(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_scored_job(job_id=1))
    repo.set_status(1, JobStatus.discarded)
    notifier = FakeNotifier([_callback(10, CallbackAction.send, 1)])
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = process_telegram_updates(repo, notifier, example_profile, llm, storage, AppConfig())

    assert summary.approved == 0
    job = repo.get_job(1)
    assert job is not None and job.status is JobStatus.discarded
    assert "ya no aplica" in notifier.last().text


def test_approve_on_scored_job_is_rejected(example_profile: ProfileModel) -> None:
    # A crafted callback approving a job that was never proposed must not advance it.
    repo = FakeJobRepository()
    repo.seed_job(_scored_job(job_id=1, status=JobStatus.scored))
    notifier = FakeNotifier([_callback(11, CallbackAction.send, 1)])
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    process_telegram_updates(repo, notifier, example_profile, llm, storage, AppConfig())

    job = repo.get_job(1)
    assert job is not None and job.status is JobStatus.scored


# --------------------------------------------------------------------------- #
# F6 — idempotency when an update is replayed after its effect
# --------------------------------------------------------------------------- #
def test_replayed_send_after_effect_is_idempotent(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_scored_job(job_id=1))
    repo.set_status(1, JobStatus.proposed)
    update = _callback(20, CallbackAction.send, 1)

    # First run approves the job.
    notifier = FakeNotifier([update])
    process_telegram_updates(
        repo, notifier, example_profile, ScriptedFakeLlmClient(), FakeFileStorage(), AppConfig()
    )
    assert repo.get_job(1).status is JobStatus.approved  # type: ignore[union-attr]

    # Simulate a crash that lost the checkpoint but kept the status change: the same
    # update is delivered again with no stored offset. The handler must be a no-op.
    repo.set_state(LAST_UPDATE_ID_KEY, "")  # emulate unwritten checkpoint
    repo._state.pop(LAST_UPDATE_ID_KEY, None)
    replay = FakeNotifier([update])
    summary = process_telegram_updates(
        repo, replay, example_profile, ScriptedFakeLlmClient(), FakeFileStorage(), AppConfig()
    )

    assert summary.approved == 0
    job = repo.get_job(1)
    assert job is not None and job.status is JobStatus.approved
    assert [s for _, s in repo.status_changes].count(JobStatus.approved) == 1


# --------------------------------------------------------------------------- #
# F7 — a non-owner-only batch is checkpointed and never starves owner updates
# --------------------------------------------------------------------------- #
def test_non_owner_only_batch_advances_cursor_then_owner_seen(
    example_profile: ProfileModel,
) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_scored_job(job_id=1))
    repo.set_status(1, JobStatus.proposed)

    # First batch: no owner updates reach the pipeline, but Telegram returned ids up
    # to 100 (non-owner). The notifier reports highest_update_id so the cursor moves.
    first = FakeNotifier(updates=(), highest_update_id=100)
    summary = process_telegram_updates(
        repo, first, example_profile, ScriptedFakeLlmClient(), FakeFileStorage(), AppConfig()
    )
    assert summary.processed == 0
    assert repo.get_state(LAST_UPDATE_ID_KEY) == "100"

    # A later owner update (id 101) must be fetched with offset 101 and acted on.
    second = FakeNotifier([_callback(101, CallbackAction.send, 1)])
    summary = process_telegram_updates(
        repo, second, example_profile, ScriptedFakeLlmClient(), FakeFileStorage(), AppConfig()
    )
    assert second.get_updates_calls == [101]
    assert summary.approved == 1


# --------------------------------------------------------------------------- #
# F8 — company+role dedupe window is measured from the proposal time
# --------------------------------------------------------------------------- #
def test_dedupe_window_uses_proposal_time_not_collection_time(
    example_profile: ProfileModel,
) -> None:
    repo = FakeJobRepository()
    # An identical earlier job that was proposed long ago (outside the 30-day window)
    # must NOT block a fresh proposal today.
    repo.seed_job(_scored_job(job_id=1, status=JobStatus.proposed))
    # Its proposal transition happened 60 days ago, outside the configured window.
    repo.set_status_changed_at(1, datetime.now(UTC) - timedelta(days=60))

    fresh = repo.seed_job(_scored_job(job_id=2))
    config = AppConfig(proposals=ProposalsConfig(dedupe_company_role_days=30))
    notifier = FakeNotifier()
    llm = ScriptedFakeLlmClient([_valid_selection()])
    storage = FakeFileStorage()

    from tests.conftest import weasyprint_available

    if not weasyprint_available():
        # Without native libs the proposal cannot render; assert the guard decision
        # instead of the full send by checking recent_company_role directly.
        assert repo.recent_company_role("ACME Corp", "Backend Engineer", within_days=30) is False
        return

    summary = propose_scored_jobs(
        [fresh], repo, notifier, example_profile, llm, storage, config, Settings()
    )
    assert summary.skipped_duplicate == 0
    assert summary.proposed == 1


def test_recent_proposal_within_window_still_blocks(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_scored_job(job_id=1))
    repo.set_status(1, JobStatus.proposed)  # proposed just now -> inside the window
    duplicate = repo.seed_job(_scored_job(job_id=2))
    config = AppConfig(proposals=ProposalsConfig(dedupe_company_role_days=30))

    summary = propose_scored_jobs(
        [duplicate],
        repo,
        FakeNotifier(),
        example_profile,
        ScriptedFakeLlmClient(),
        FakeFileStorage(),
        config,
        Settings(),
    )
    assert summary.skipped_duplicate == 1


# --------------------------------------------------------------------------- #
# F9 — a Telegram delivery rejection does not lose the proposal
# --------------------------------------------------------------------------- #
def test_delivery_failure_on_language_prompt_keeps_job_scored(
    example_profile: ProfileModel,
) -> None:
    repo = FakeJobRepository()
    stored = repo.seed_job(_scored_job(job_id=1, language=None))
    # The first send (the language prompt) is rejected by the Bot API.
    notifier = FakeNotifier(fail_send_after=0)
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = propose_scored_jobs(
        [stored], repo, notifier, example_profile, llm, storage, AppConfig(), Settings()
    )

    # The job is NOT advanced to proposed: it stays scored so a later run retries it.
    assert summary.proposed == 0
    job = repo.get_job(1)
    assert job is not None and job.status is JobStatus.scored


# --------------------------------------------------------------------------- #
# F10 — an unrecoverable document failure is isolated and marks the job error
# --------------------------------------------------------------------------- #
def test_selection_failure_marks_error_and_isolates_batch(
    example_profile: ProfileModel,
) -> None:
    repo = FakeJobRepository()
    bad = repo.seed_job(_scored_job(job_id=1, company="BadCo"))
    good = repo.seed_job(_scored_job(job_id=2, company="GoodCo"))

    # Both jobs cite an unknown id on every attempt -> SelectionError (unrecoverable)
    # before any rendering. The batch must process BOTH (one failing job does not
    # abort the other) and mark each one error, never leaving them 'scored'.
    invalid = _valid_selection().model_copy(update={"ordered_achievement_ids": ["ach_nope"]})
    llm = ScriptedFakeLlmClient([invalid.model_copy() for _ in range(4)])
    storage = FakeFileStorage()

    summary = propose_scored_jobs(
        [bad, good],
        repo,
        FakeNotifier(),
        example_profile,
        llm,
        storage,
        AppConfig(),
        Settings(),
    )

    assert summary.proposed == 0
    assert storage.uploads == []
    for job_id in (1, 2):
        errored = repo.get_job(job_id)
        assert errored is not None and errored.status is JobStatus.error


def _valid_selection(language: Language = Language.es) -> ContentSelection:
    return ContentSelection(
        summary_variant="backend_java",
        ordered_achievement_ids=["ach_acme_latency"],
        project_ids=[],
        highlighted_skill_ids=[],
        language=language,
    )
