"""Post-approval send flow: email vs manual, recording, and no double-send."""

from __future__ import annotations

import json

from jobpilot.channels.telegram import CallbackAction, encode_callback
from jobpilot.config import AppConfig, Language
from jobpilot.interfaces import Update
from jobpilot.llm.schemas import CoverLetterDraft
from jobpilot.models import (
    ApplicationChannel,
    ContentSelection,
    Job,
    JobAnalysis,
    JobStatus,
)
from jobpilot.pipeline.run import (
    PROPOSAL_ARTIFACTS_PREFIX,
    process_telegram_updates,
)
from jobpilot.profile.model import ProfileModel
from tests.conftest import skip_without_weasyprint
from tests.fakes import (
    FakeFileStorage,
    FakeJobRepository,
    FakeMailer,
    FakeNotifier,
    ScriptedFakeLlmClient,
)


def _proposed_job(*, job_id: int, contact_email: str | None) -> Job:
    return Job(
        id=job_id,
        source="remoteok",
        external_id=f"rok-{job_id}",
        url=f"https://example.com/job/{job_id}",
        apply_url=f"https://example.com/apply/{job_id}",
        title="Backend Engineer",
        company="ACME Corp",
        contact_email=contact_email,
        language=Language.en,
        score=90,
        status=JobStatus.proposed,
        analysis=JobAnalysis(normalized_title="Backend Engineer", language=Language.en),
    )


def _seed_artifacts(repo: FakeJobRepository, job_id: int) -> None:
    """Pre-store the proposal artifacts a real proposal would have persisted."""
    repo.set_state(
        PROPOSAL_ARTIFACTS_PREFIX + str(job_id),
        json.dumps(
            {
                "language": "en",
                "cv_variant": "backend_java",
                "cv_url": "https://drive.example.com/file/cv",
                "cover_letter_url": "https://drive.example.com/file/cover",
            }
        ),
    )


def _callback(update_id: int, action: CallbackAction, job_id: int) -> Update:
    return Update(update_id=update_id, chat_id="1", callback_data=encode_callback(action, job_id))


def _status(repo: FakeJobRepository, job_id: int) -> JobStatus:
    job = repo.get_job(job_id)
    assert job is not None
    return job.status


def _selection() -> ContentSelection:
    return ContentSelection(
        summary_variant="backend_java",
        ordered_achievement_ids=["ach_acme_latency"],
        project_ids=[],
        highlighted_skill_ids=[],
        language=Language.en,
    )


def _cover() -> CoverLetterDraft:
    return CoverLetterDraft(body="I am excited to bring my backend experience to your team.")


# --------------------------------------------------------------------------- #
# Email path
# --------------------------------------------------------------------------- #
@skip_without_weasyprint
def test_send_with_contact_email_records_email_channel(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=1, contact_email="recruiter@acme.test"))
    _seed_artifacts(repo, 1)
    notifier = FakeNotifier([_callback(10, CallbackAction.send, 1)])
    mailer = FakeMailer()
    # The email path regenerates the tailored documents to attach them.
    llm = ScriptedFakeLlmClient([_selection(), _cover()])
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    assert summary.applied == 1
    # Email sent with both PDF attachments to the contact address.
    assert len(mailer.sent) == 1
    assert mailer.sent[0].to == "recruiter@acme.test"
    assert len(mailer.sent[0].attachments) == 2
    # Job advanced to applied and the application was recorded as email.
    assert _status(repo, 1) is JobStatus.applied
    assert len(repo.applications) == 1
    app = repo.applications[0]
    assert app.channel is ApplicationChannel.email
    assert app.language is Language.en
    assert app.cv_variant == "backend_java"
    assert app.applied_at is not None
    assert str(app.cv_url).startswith("https://drive.example.com/file")


@skip_without_weasyprint
def test_second_send_does_not_resend(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=1, contact_email="recruiter@acme.test"))
    _seed_artifacts(repo, 1)
    # Two identical Enviar callbacks are queued, but only the first should send.
    notifier = FakeNotifier(
        [_callback(10, CallbackAction.send, 1), _callback(11, CallbackAction.send, 1)]
    )
    mailer = FakeMailer()
    llm = ScriptedFakeLlmClient([_selection(), _cover()])
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    # Idempotent: the second Enviar finds the job already applied and does nothing.
    assert summary.applied == 1
    assert len(mailer.sent) == 1
    assert len(repo.applications) == 1


@skip_without_weasyprint
def test_failed_email_does_not_record_application(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=1, contact_email="recruiter@acme.test"))
    _seed_artifacts(repo, 1)
    notifier = FakeNotifier([_callback(10, CallbackAction.send, 1)])
    mailer = FakeMailer(fail=True)
    llm = ScriptedFakeLlmClient([_selection(), _cover()])
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    # A rejected send records nothing and leaves the job proposed for a later retry.
    assert summary.applied == 0
    assert repo.applications == []
    assert _status(repo, 1) is JobStatus.proposed


# --------------------------------------------------------------------------- #
# Manual path (no contact email)
# --------------------------------------------------------------------------- #
def test_send_without_contact_email_enters_manual_mode(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=2, contact_email=None))
    _seed_artifacts(repo, 2)
    notifier = FakeNotifier([_callback(20, CallbackAction.send, 2)])
    mailer = FakeMailer()
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    # No email, no regeneration; the job waits in approved with a "Ya apliqué" button.
    assert summary.approved == 1
    assert mailer.sent == []
    assert storage.uploads == []
    assert _status(repo, 2) is JobStatus.approved
    assert repo.applications == []
    manual_msg = notifier.last()
    assert "manualmente" in manual_msg.text
    applied_cb = encode_callback(CallbackAction.applied, 2)
    assert any(b.callback_data == applied_cb for b in manual_msg.buttons)


def test_manual_mode_then_ya_aplique_records_manual(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=2, contact_email=None))
    _seed_artifacts(repo, 2)
    notifier = FakeNotifier(
        [
            _callback(20, CallbackAction.send, 2),
            _callback(21, CallbackAction.applied, 2),
        ]
    )
    mailer = FakeMailer()
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    assert summary.approved == 1
    assert summary.applied == 1
    assert _status(repo, 2) is JobStatus.applied
    assert len(repo.applications) == 1
    app = repo.applications[0]
    assert app.channel is ApplicationChannel.manual
    assert app.language is Language.en
    assert app.cv_variant == "backend_java"
    assert str(app.cv_url).startswith("https://drive.example.com/file")


def test_ya_aplique_straight_from_proposal_records_manual(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=3, contact_email="recruiter@acme.test"))
    _seed_artifacts(repo, 3)
    notifier = FakeNotifier([_callback(30, CallbackAction.applied, 3)])
    mailer = FakeMailer()
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    # The owner applied on the portal themselves: no email, recorded as manual.
    assert summary.applied == 1
    assert mailer.sent == []
    assert _status(repo, 3) is JobStatus.applied
    assert repo.applications[0].channel is ApplicationChannel.manual
