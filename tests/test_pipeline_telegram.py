"""Telegram orchestration: idempotency, proposal guards and status transitions.

Document rendering is avoided here by exercising the paths that change status
without regenerating documents (approval callbacks) and the guard paths that skip a
job before any rendering, so these tests run without WeasyPrint's native libraries.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jobpilot.channels.telegram import CallbackAction, encode_callback
from jobpilot.config import AppConfig, Language, ProposalsConfig, Settings
from jobpilot.interfaces import Update
from jobpilot.models import (
    Application,
    ApplicationChannel,
    Job,
    JobAnalysis,
    JobStatus,
)
from jobpilot.pipeline.run import (
    LAST_UPDATE_ID_KEY,
    process_telegram_updates,
    propose_scored_jobs,
)
from jobpilot.profile.model import ProfileModel
from tests.fakes import FakeFileStorage, FakeJobRepository, FakeNotifier, ScriptedFakeLlmClient


def _scored_job(*, job_id: int, company: str = "ACME Corp", title: str = "Backend Engineer") -> Job:
    return Job(
        id=job_id,
        source="remoteok",
        external_id=f"rok-{job_id}",
        url=f"https://example.com/job/{job_id}",
        title=title,
        company=company,
        language=Language.es,
        score=90,
        status=JobStatus.scored,
        analysis=JobAnalysis(normalized_title=title),
    )


def _callback(update_id: int, action: CallbackAction, job_id: int) -> Update:
    return Update(update_id=update_id, chat_id="1", callback_data=encode_callback(action, job_id))


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_update_processed_once_by_update_id(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_scored_job(job_id=1))
    repo.set_status(1, JobStatus.proposed)
    notifier = FakeNotifier([_callback(30, CallbackAction.send, 1)])
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    first = process_telegram_updates(repo, notifier, example_profile, llm, storage, AppConfig())
    assert first.processed == 1
    assert first.approved == 1
    assert repo.get_state(LAST_UPDATE_ID_KEY) == "30"

    # The same update is still queued; a second run must not act on it again.
    second = process_telegram_updates(repo, notifier, example_profile, llm, storage, AppConfig())
    assert second.processed == 0
    assert second.approved == 0
    # Only the single approval status change was recorded.
    assert [s for _, s in repo.status_changes].count(JobStatus.approved) == 1


# --------------------------------------------------------------------------- #
# Status transitions
# --------------------------------------------------------------------------- #
def test_status_transitions_for_each_action(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    for job_id in (1, 2, 3):
        repo.seed_job(_scored_job(job_id=job_id, company=f"Co{job_id}"))
        repo.set_status(job_id, JobStatus.proposed)
    notifier = FakeNotifier(
        [
            _callback(40, CallbackAction.send, 1),
            _callback(41, CallbackAction.discard, 2),
            _callback(42, CallbackAction.applied, 3),
        ]
    )
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = process_telegram_updates(repo, notifier, example_profile, llm, storage, AppConfig())

    assert (summary.approved, summary.discarded, summary.applied) == (1, 1, 1)
    statuses = {job.id: job.status for job in repo.jobs}
    assert statuses[1] is JobStatus.approved
    assert statuses[2] is JobStatus.discarded
    assert statuses[3] is JobStatus.applied


# --------------------------------------------------------------------------- #
# Proposal guards
# --------------------------------------------------------------------------- #
def test_daily_limit_blocks_extra_proposals(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    jobs = [repo.seed_job(_scored_job(job_id=i, company=f"Co{i}")) for i in (1, 2)]
    notifier = FakeNotifier()
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = propose_scored_jobs(
        jobs,
        repo,
        notifier,
        example_profile,
        llm,
        storage,
        AppConfig(),
        Settings(daily_application_limit=0),
    )

    assert summary.proposed == 0
    assert summary.skipped_daily_limit == 2
    # Nothing rendered or sent while the daily limit is exhausted.
    assert storage.uploads == []
    assert notifier.messages == []


def test_daily_limit_counts_applications_already_made_today(
    example_profile: ProfileModel,
) -> None:
    repo = FakeJobRepository()
    repo.create_application(
        Application(
            job_id=99,
            language=Language.es,
            cv_variant="backend_java",
            channel=ApplicationChannel.email,
            applied_at=datetime.now(UTC),
        )
    )
    jobs = [repo.seed_job(_scored_job(job_id=1))]
    notifier = FakeNotifier()
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = propose_scored_jobs(
        jobs,
        repo,
        notifier,
        example_profile,
        llm,
        storage,
        AppConfig(),
        Settings(daily_application_limit=1),
        today=datetime.now(UTC).date(),
    )

    # One application already today fills the limit of 1, so the scored job is held.
    assert summary.proposed == 0
    assert summary.skipped_daily_limit == 1


def test_company_role_guard_blocks_and_discards(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    # An earlier proposal for the same company+role already exists.
    repo.seed_job(_scored_job(job_id=1))
    repo.set_status(1, JobStatus.proposed)
    duplicate = repo.seed_job(_scored_job(job_id=2))
    notifier = FakeNotifier()
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()
    config = AppConfig(proposals=ProposalsConfig(dedupe_company_role_days=30))

    summary = propose_scored_jobs(
        [duplicate],
        repo,
        notifier,
        example_profile,
        llm,
        storage,
        config,
        Settings(daily_application_limit=5),
    )

    assert summary.skipped_duplicate == 1
    assert summary.proposed == 0
    discarded = repo.get_job(2)
    assert discarded is not None
    assert discarded.status is JobStatus.discarded
    assert storage.uploads == []


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def test_pendientes_and_estado_report_in_spanish(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_scored_job(job_id=1))
    repo.set_status(1, JobStatus.proposed)
    notifier = FakeNotifier(
        [
            Update(update_id=50, chat_id="1", text="/pendientes"),
            Update(update_id=51, chat_id="1", text="/estado"),
        ]
    )
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = process_telegram_updates(repo, notifier, example_profile, llm, storage, AppConfig())

    assert summary.commands == 2
    pending_msg = notifier.messages[0].text
    status_msg = notifier.messages[1].text
    assert "Ofertas pendientes" in pending_msg
    assert "ACME Corp" in pending_msg
    # R16.2: the status report shows Spanish labels, never the English enum values.
    assert "propuestas" in status_msg
    assert "proposed" not in status_msg
