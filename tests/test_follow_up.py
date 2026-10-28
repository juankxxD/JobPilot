"""Phase 7 follow-up: reminder due date, reminders, outcome commands, weekly report.

Everything runs against the in-memory ``FakeJobRepository`` and ``FakeNotifier``
with the clock injected, so no live Telegram/DB/clock is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobpilot.config import AppConfig, FollowUpConfig, Language
from jobpilot.interfaces import Update
from jobpilot.models import (
    Application,
    ApplicationChannel,
    ApplicationOutcome,
    Job,
    JobStatus,
)
from jobpilot.pipeline.follow_up import (
    LAST_WEEKLY_REPORT_KEY,
    compute_follow_up_at,
    record_outcome,
    send_due_reminders,
    send_weekly_report,
)
from jobpilot.pipeline.run import process_telegram_updates
from jobpilot.profile.model import ProfileModel
from tests.fakes import (
    FakeFileStorage,
    FakeJobRepository,
    FakeNotifier,
    ScriptedFakeLlmClient,
)

# A Monday, used as the deterministic "now" for weekly-report tests.
MONDAY = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)  # isoweekday() == 1
TUESDAY = datetime(2024, 1, 2, 9, 0, tzinfo=UTC)  # isoweekday() == 2


def _applied_application(
    *,
    job_id: int,
    applied_at: datetime,
    follow_up_at: datetime | None,
    outcome: ApplicationOutcome = ApplicationOutcome.pending,
    reminder_sent_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Application:
    return Application(
        job_id=job_id,
        language=Language.en,
        cv_variant="backend_java",
        channel=ApplicationChannel.email,
        applied_at=applied_at,
        follow_up_at=follow_up_at,
        outcome=outcome,
        reminder_sent_at=reminder_sent_at,
        created_at=created_at,
    )


# --------------------------------------------------------------------------- #
# follow_up_at computation
# --------------------------------------------------------------------------- #
def test_compute_follow_up_at_uses_default_seven_days() -> None:
    applied = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    assert compute_follow_up_at(applied, FollowUpConfig()) == applied + timedelta(days=7)


def test_compute_follow_up_at_honours_configured_days() -> None:
    applied = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    config = FollowUpConfig(reminder_after_days=3)
    assert compute_follow_up_at(applied, config) == applied + timedelta(days=3)


def test_send_flow_stamps_follow_up_at_on_recorded_application(
    example_profile: ProfileModel,
) -> None:
    """The manual send flow records an application with a computed follow_up_at."""
    repo = FakeJobRepository()
    repo.seed_job(
        Job(
            id=1,
            source="remoteok",
            external_id="rok-1",
            url="https://example.com/job/1",
            title="Backend Engineer",
            company="ACME Corp",
            language=Language.en,
            status=JobStatus.proposed,
        )
    )
    # "Ya apliqué" straight from a proposal records a manual application.
    notifier = FakeNotifier([Update(update_id=5, chat_id="1", callback_data="applied:1")])
    process_telegram_updates(
        repo,
        notifier,
        example_profile,
        ScriptedFakeLlmClient(),
        FakeFileStorage(),
        AppConfig(),
    )
    assert len(repo.applications) == 1
    app = repo.applications[0]
    assert app.applied_at is not None
    assert app.follow_up_at == app.applied_at + timedelta(days=7)


# --------------------------------------------------------------------------- #
# Due reminders
# --------------------------------------------------------------------------- #
def test_due_reminder_sent_once_and_not_repeated() -> None:
    repo = FakeJobRepository()
    applied = datetime(2024, 1, 1, tzinfo=UTC)
    repo.create_application(
        _applied_application(job_id=1, applied_at=applied, follow_up_at=applied + timedelta(days=7))
    )
    notifier = FakeNotifier()
    now = applied + timedelta(days=8)  # past the follow-up date

    assert send_due_reminders(repo, notifier, now=now) == 1
    assert len(notifier.messages) == 1
    assert "Recordatorio de seguimiento" in notifier.messages[0].text
    # The application is now marked as reminded; a second run sends nothing.
    assert repo.applications[0].reminder_sent_at == now
    assert send_due_reminders(repo, notifier, now=now + timedelta(days=1)) == 0
    assert len(notifier.messages) == 1


def test_reminder_not_sent_before_follow_up_date() -> None:
    repo = FakeJobRepository()
    applied = datetime(2024, 1, 1, tzinfo=UTC)
    repo.create_application(
        _applied_application(job_id=1, applied_at=applied, follow_up_at=applied + timedelta(days=7))
    )
    notifier = FakeNotifier()
    # Only five days later: not yet due.
    assert send_due_reminders(repo, notifier, now=applied + timedelta(days=5)) == 0
    assert notifier.messages == []


def test_reminder_skips_applications_with_an_outcome() -> None:
    repo = FakeJobRepository()
    applied = datetime(2024, 1, 1, tzinfo=UTC)
    repo.create_application(
        _applied_application(
            job_id=1,
            applied_at=applied,
            follow_up_at=applied + timedelta(days=7),
            outcome=ApplicationOutcome.interview,
        )
    )
    notifier = FakeNotifier()
    assert send_due_reminders(repo, notifier, now=applied + timedelta(days=30)) == 0
    assert notifier.messages == []


# --------------------------------------------------------------------------- #
# Outcome transitions
# --------------------------------------------------------------------------- #
def test_record_outcome_sets_interview() -> None:
    repo = FakeJobRepository()
    applied = datetime(2024, 1, 1, tzinfo=UTC)
    repo.create_application(_applied_application(job_id=7, applied_at=applied, follow_up_at=None))
    notifier = FakeNotifier()

    assert record_outcome(repo, notifier, 7, ApplicationOutcome.interview) is True
    assert repo.applications[0].outcome is ApplicationOutcome.interview
    assert "entrevista" in notifier.messages[0].text.lower()


def test_record_outcome_sets_rejected() -> None:
    repo = FakeJobRepository()
    applied = datetime(2024, 1, 1, tzinfo=UTC)
    repo.create_application(_applied_application(job_id=7, applied_at=applied, follow_up_at=None))
    notifier = FakeNotifier()

    assert record_outcome(repo, notifier, 7, ApplicationOutcome.rejected) is True
    assert repo.applications[0].outcome is ApplicationOutcome.rejected
    assert "rechazo" in notifier.messages[0].text.lower()


def test_record_outcome_without_application_reports_and_changes_nothing() -> None:
    repo = FakeJobRepository()
    notifier = FakeNotifier()
    assert record_outcome(repo, notifier, 99, ApplicationOutcome.interview) is False
    assert "99" in notifier.messages[0].text


def test_entrevista_and_rechazo_commands_transition_outcomes(
    example_profile: ProfileModel,
) -> None:
    """The /entrevista and /rechazo commands flow through to outcome changes."""
    repo = FakeJobRepository()
    applied = datetime(2024, 1, 1, tzinfo=UTC)
    repo.seed_job(_minimal_job(job_id=3))
    repo.seed_job(_minimal_job(job_id=4))
    repo.create_application(_applied_application(job_id=3, applied_at=applied, follow_up_at=None))
    repo.create_application(_applied_application(job_id=4, applied_at=applied, follow_up_at=None))
    notifier = FakeNotifier(
        [
            Update(update_id=1, chat_id="1", text="/entrevista 3"),
            Update(update_id=2, chat_id="1", text="/rechazo 4"),
        ]
    )

    process_telegram_updates(
        repo,
        notifier,
        example_profile,
        ScriptedFakeLlmClient(),
        FakeFileStorage(),
        AppConfig(),
    )

    outcomes = {app.job_id: app.outcome for app in repo.applications}
    assert outcomes[3] is ApplicationOutcome.interview
    assert outcomes[4] is ApplicationOutcome.rejected


# --------------------------------------------------------------------------- #
# Weekly report
# --------------------------------------------------------------------------- #
def test_weekly_report_sent_only_on_configured_weekday() -> None:
    repo = FakeJobRepository()
    notifier = FakeNotifier()
    config = FollowUpConfig(weekly_report_weekday=1)  # Monday

    # Tuesday: not the configured day, nothing is sent.
    assert send_weekly_report(repo, notifier, config, now=TUESDAY) is False
    assert notifier.messages == []

    # Monday: the report goes out.
    assert send_weekly_report(repo, notifier, config, now=MONDAY) is True
    assert len(notifier.messages) == 1
    assert "Reporte semanal" in notifier.messages[0].text


def test_weekly_report_sent_once_per_day() -> None:
    repo = FakeJobRepository()
    notifier = FakeNotifier()
    config = FollowUpConfig(weekly_report_weekday=1)

    assert send_weekly_report(repo, notifier, config, now=MONDAY) is True
    # A second run on the same Monday (e.g. the 11:00 pipeline) does not resend.
    assert send_weekly_report(repo, notifier, config, now=MONDAY.replace(hour=11)) is False
    assert len(notifier.messages) == 1
    assert repo.get_state(LAST_WEEKLY_REPORT_KEY) == MONDAY.date().isoformat()


def test_weekly_report_summarises_the_week_outcomes() -> None:
    repo = FakeJobRepository()
    within = MONDAY - timedelta(days=2)
    old = MONDAY - timedelta(days=10)  # outside the 7-day window
    repo.create_application(
        _applied_application(
            job_id=1,
            applied_at=within,
            follow_up_at=None,
            outcome=ApplicationOutcome.interview,
            created_at=within,
        )
    )
    repo.create_application(
        _applied_application(
            job_id=2,
            applied_at=within,
            follow_up_at=None,
            outcome=ApplicationOutcome.pending,
            created_at=within,
        )
    )
    repo.create_application(
        _applied_application(
            job_id=3,
            applied_at=old,
            follow_up_at=None,
            outcome=ApplicationOutcome.rejected,
            created_at=old,
        )
    )
    notifier = FakeNotifier()
    config = FollowUpConfig(weekly_report_weekday=1)

    assert send_weekly_report(repo, notifier, config, now=MONDAY) is True
    text = notifier.messages[0].text
    # Only the two in-window applications are counted; the 10-day-old one is excluded.
    assert "Total de la semana: 2" in text
    assert "entrevistas: 1" in text
    assert "rechazos" not in text


def _minimal_job(*, job_id: int) -> Job:
    return Job(
        id=job_id,
        source="remoteok",
        external_id=f"rok-{job_id}",
        url=f"https://example.com/job/{job_id}",
        title="Backend Engineer",
        company="ACME Corp",
        language=Language.en,
        status=JobStatus.applied,
    )
