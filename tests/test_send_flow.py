"""Post-approval send flow: email vs manual, recording, and no double-send."""

from __future__ import annotations

import base64
import json

from jobpilot.channels.telegram import CallbackAction, encode_callback
from jobpilot.config import AppConfig, Language
from jobpilot.interfaces import Update
from jobpilot.models import (
    ApplicationChannel,
    Job,
    JobAnalysis,
    JobStatus,
)
from jobpilot.pipeline.run import (
    PROPOSAL_ARTIFACTS_PREFIX,
    process_telegram_updates,
)
from jobpilot.profile.model import ProfileModel
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


CV_BYTES = b"%PDF-1.4 approved-cv"
COVER_BYTES = b"%PDF-1.4 approved-cover"


def _seed_artifacts(repo: FakeJobRepository, job_id: int, *, with_bytes: bool = True) -> None:
    """Pre-store the proposal artifacts a real proposal would have persisted.

    Includes the exact approved PDF bytes (base64) so the email path attaches them
    without regenerating. ``with_bytes=False`` reproduces a legacy/seeded proposal
    that stored no bytes, which the email path must refuse rather than regenerate.
    """
    payload: dict[str, object] = {
        "language": "en",
        "cv_variant": "backend_java",
        "cv_url": "https://drive.example.com/file/cv",
        "cover_letter_url": "https://drive.example.com/file/cover",
    }
    if with_bytes:
        payload["cv"] = {
            "filename": "CV_EN.pdf",
            "mime_type": "application/pdf",
            "content_b64": base64.b64encode(CV_BYTES).decode("ascii"),
        }
        payload["cover_letter"] = {
            "filename": "Cover_EN.pdf",
            "mime_type": "application/pdf",
            "content_b64": base64.b64encode(COVER_BYTES).decode("ascii"),
        }
    repo.set_state(PROPOSAL_ARTIFACTS_PREFIX + str(job_id), json.dumps(payload))


def _callback(update_id: int, action: CallbackAction, job_id: int) -> Update:
    return Update(update_id=update_id, chat_id="1", callback_data=encode_callback(action, job_id))


def _status(repo: FakeJobRepository, job_id: int) -> JobStatus:
    job = repo.get_job(job_id)
    assert job is not None
    return job.status


# --------------------------------------------------------------------------- #
# Email path
#
# The email path attaches the EXACT approved PDF bytes stored at proposal time and
# never regenerates documents, so no WeasyPrint render and no LLM call happen here.
# --------------------------------------------------------------------------- #
def test_send_with_contact_email_records_email_channel(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=1, contact_email="recruiter@acme.test"))
    _seed_artifacts(repo, 1)
    notifier = FakeNotifier([_callback(10, CallbackAction.send, 1)])
    mailer = FakeMailer()
    llm = ScriptedFakeLlmClient()  # no responses: the email path must not call the LLM
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    assert summary.applied == 1
    # Email sent with both PDF attachments to the contact address, no regeneration.
    assert len(mailer.sent) == 1
    assert mailer.sent[0].to == "recruiter@acme.test"
    assert mailer.sent[0].attachments == ["CV_EN.pdf", "Cover_EN.pdf"]
    assert llm.prompts == []
    assert storage.uploads == []
    # Job advanced to applied and the application was recorded as email.
    assert _status(repo, 1) is JobStatus.applied
    assert len(repo.applications) == 1
    app = repo.applications[0]
    assert app.channel is ApplicationChannel.email
    assert app.language is Language.en
    assert app.cv_variant == "backend_java"
    assert app.applied_at is not None
    # The recorded URLs are exactly the approved proposal artifact URLs.
    assert str(app.cv_url) == "https://drive.example.com/file/cv"
    assert str(app.cover_letter_url) == "https://drive.example.com/file/cover"
    assert repo.email_claim_state(1) == "done"


def test_email_sends_the_exact_approved_document_bytes(example_profile: ProfileModel) -> None:
    """The emailed PDF bytes are byte-identical to the approved proposal artifacts."""
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=1, contact_email="recruiter@acme.test"))
    _seed_artifacts(repo, 1)
    notifier = FakeNotifier([_callback(10, CallbackAction.send, 1)])
    mailer = FakeMailer(record_bytes=True)
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    process_telegram_updates(repo, notifier, example_profile, llm, storage, AppConfig(), mailer)

    sent_bytes = mailer.sent[0].attachment_bytes
    assert sent_bytes == [CV_BYTES, COVER_BYTES]


def test_email_without_approved_bytes_refuses_to_regenerate(
    example_profile: ProfileModel,
) -> None:
    """A proposal that stored no approved bytes is refused, never regenerated."""
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=1, contact_email="recruiter@acme.test"))
    _seed_artifacts(repo, 1, with_bytes=False)
    notifier = FakeNotifier([_callback(10, CallbackAction.send, 1)])
    mailer = FakeMailer()
    llm = ScriptedFakeLlmClient()  # must not be called
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    assert summary.applied == 0
    assert mailer.sent == []
    assert llm.prompts == []
    assert repo.applications == []
    # The job stays proposed and no claim was taken, so re-proposing can recover it.
    assert _status(repo, 1) is JobStatus.proposed
    assert repo.email_claim_state(1) is None
    assert "aprobados" in notifier.last().text


def test_second_send_does_not_resend(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=1, contact_email="recruiter@acme.test"))
    _seed_artifacts(repo, 1)
    # Two identical Enviar callbacks are queued, but only the first should send.
    notifier = FakeNotifier(
        [_callback(10, CallbackAction.send, 1), _callback(11, CallbackAction.send, 1)]
    )
    mailer = FakeMailer()
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    # Idempotent: the second Enviar finds the job already applied and does nothing.
    assert summary.applied == 1
    assert len(mailer.sent) == 1
    assert len(repo.applications) == 1


def test_failed_email_does_not_record_application(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_proposed_job(job_id=1, contact_email="recruiter@acme.test"))
    _seed_artifacts(repo, 1)
    notifier = FakeNotifier([_callback(10, CallbackAction.send, 1)])
    mailer = FakeMailer(fail=True)
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    # A rejected send records nothing and leaves the job proposed for a later retry.
    assert summary.applied == 0
    assert repo.applications == []
    assert _status(repo, 1) is JobStatus.proposed
    # The claim was released, so a later Enviar can cleanly re-claim and retry.
    assert repo.email_claim_state(1) is None


def test_database_failure_after_send_blocks_resend(example_profile: ProfileModel) -> None:
    """A DB failure after the provider accepted must not resend or record twice.

    The first Enviar sends the email, then finalization fails (simulated DB error).
    The job stays proposed but the claim is left in_progress, so a second Enviar
    cannot invoke the provider again — no duplicate email, no duplicate application.
    """
    repo = FakeJobRepository()
    repo.fail_finalize = True
    repo.seed_job(_proposed_job(job_id=1, contact_email="recruiter@acme.test"))
    _seed_artifacts(repo, 1)
    notifier = FakeNotifier(
        [_callback(10, CallbackAction.send, 1), _callback(11, CallbackAction.send, 1)]
    )
    mailer = FakeMailer()
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = process_telegram_updates(
        repo, notifier, example_profile, llm, storage, AppConfig(), mailer
    )

    # The provider was called exactly once; the ambiguous claim blocks the resend.
    assert summary.applied == 0
    assert len(mailer.sent) == 1
    assert repo.applications == []
    assert _status(repo, 1) is JobStatus.proposed
    assert repo.email_claim_state(1) == "in_progress"


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
