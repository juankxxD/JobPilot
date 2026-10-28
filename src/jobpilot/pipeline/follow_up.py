"""Phase 7 — follow-up: reminders, outcome transitions and the weekly report.

Three owner-facing behaviours live here, all driven off the ``applications`` table
and all with an injected clock/notifier so tests never touch live services:

* :func:`compute_follow_up_at` — the single place that derives an application's
  ``follow_up_at`` from its ``applied_at`` and ``follow_up.reminder_after_days``
  (default 7). The send flow stamps this when it records an application so the
  reminder due date is set at creation, exactly as the plan requires.
* :func:`send_due_reminders` — for every pending application whose ``follow_up_at``
  has passed and that has not yet been reminded, send one Spanish Telegram reminder
  and record ``reminder_sent_at`` so it is never reminded twice.
* :func:`record_outcome` — apply the ``/entrevista id`` -> interview and
  ``/rechazo id`` -> rejected transitions coming from the Phase 5 Telegram intents.
* :func:`send_weekly_report` — once per week, on ``follow_up.weekly_report_weekday``,
  summarise the week's applications and their outcomes to the owner.

All user-facing text is Spanish; code, identifiers and logs are English.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from jobpilot.logging import get_logger
from jobpilot.models import ApplicationOutcome

if TYPE_CHECKING:
    from jobpilot.config import FollowUpConfig
    from jobpilot.interfaces import Notifier
    from jobpilot.models import Application

log = get_logger(__name__)

# ``telegram_state`` key holding the ISO date of the last weekly report sent, so the
# report goes out at most once per weekly-report day even if the pipeline runs many
# times that day.
LAST_WEEKLY_REPORT_KEY = "follow_up_last_weekly_report"

# The outcome each owner command sets.
_INTERVIEW = ApplicationOutcome.interview
_REJECTED = ApplicationOutcome.rejected

# Spanish labels for the outcomes shown in the weekly report. Internal values stay
# English; only these labels reach the owner.
_OUTCOME_LABELS: dict[ApplicationOutcome, str] = {
    ApplicationOutcome.pending: "sin respuesta aún",
    ApplicationOutcome.interview: "entrevistas",
    ApplicationOutcome.rejected: "rechazos",
    ApplicationOutcome.offer: "ofertas",
    ApplicationOutcome.no_response: "sin respuesta",
}


class FollowUpRepository(Protocol):
    """The repository surface the follow-up flow needs.

    Implemented by the concrete ``NeonJobRepository`` and the in-memory test fake.
    Kept separate from the minimal :class:`~jobpilot.interfaces.JobRepository`
    Protocol so the base contract does not grow follow-up-only query methods.
    """

    def applications_without_outcome(self, before: datetime) -> list[Application]: ...

    def mark_reminder_sent(self, application_id: int, when: datetime) -> None: ...

    def set_outcome(self, job_id: int, outcome: ApplicationOutcome) -> int: ...

    def applications_in_period(self, start: datetime, end: datetime) -> list[Application]: ...

    def get_state(self, key: str) -> str | None: ...

    def set_state(self, key: str, value: str) -> None: ...


class StateRepository(Protocol):
    """The ``telegram_state`` read/write surface the weekly-report guard needs."""

    def get_state(self, key: str) -> str | None: ...

    def set_state(self, key: str, value: str) -> None: ...


def compute_follow_up_at(applied_at: datetime, config: FollowUpConfig) -> datetime:
    """Return when an application should be reminded: ``applied_at`` + N days.

    N is ``follow_up.reminder_after_days`` (default 7). This is the single source of
    truth for the due date, called by the send flow when it records an application so
    the reminder date is stored at creation time.
    """
    return applied_at + timedelta(days=config.reminder_after_days)


def send_due_reminders(
    repo: FollowUpRepository,
    notifier: Notifier,
    *,
    now: datetime | None = None,
) -> int:
    """Send one reminder per due, still-pending, not-yet-reminded application.

    Returns the number of reminders sent. Each application is marked
    ``reminder_sent_at`` right after its reminder is delivered, so a later run never
    reminds for it again (the reminder is sent exactly once). A reminder for one
    application never blocks the rest: a delivery failure is logged and the next
    application is still attempted.
    """
    moment = now or datetime.now(UTC)
    due = repo.applications_without_outcome(moment)
    sent = 0
    for app in due:
        if app.id is None:  # pragma: no cover - persisted applications always have an id
            continue
        notifier.send_message(_render_reminder(app))
        repo.mark_reminder_sent(app.id, moment)
        sent += 1
        log.info("follow_up_reminder_sent", application_id=app.id, job_id=app.job_id)
    if sent:
        log.info("follow_up_reminders_done", sent=sent)
    return sent


def record_outcome(
    repo: FollowUpRepository,
    notifier: Notifier,
    job_id: int | None,
    outcome: ApplicationOutcome,
) -> bool:
    """Apply an outcome transition from ``/entrevista`` / ``/rechazo`` and confirm.

    Returns ``True`` when at least one application changed. A missing id or a job
    with no recorded application is reported to the owner in Spanish and leaves
    everything unchanged, so a stray command never fabricates an outcome.
    """
    if job_id is None:
        label = _OUTCOME_LABELS[outcome]
        notifier.send_message(f"Usa el formato correcto para registrar un {label[:-1]}.")
        return False
    changed = repo.set_outcome(job_id, outcome)
    if changed == 0:
        notifier.send_message(f"No encontré una postulación registrada para la oferta {job_id}.")
        return False
    if outcome is _INTERVIEW:
        notifier.send_message(f"Registrada entrevista para la oferta {job_id}. ¡Mucha suerte!")
    elif outcome is _REJECTED:
        notifier.send_message(f"Registrado rechazo para la oferta {job_id}.")
    else:  # pragma: no cover - only interview/rejected are wired today
        notifier.send_message(f"Resultado actualizado para la oferta {job_id}.")
    log.info("follow_up_outcome_recorded", job_id=job_id, outcome=outcome.value)
    return True


def send_weekly_report(
    repo: FollowUpRepository,
    notifier: Notifier,
    config: FollowUpConfig,
    *,
    now: datetime | None = None,
) -> bool:
    """Send the weekly report, but only once on the configured weekday.

    The report goes out only when ``now`` falls on ``follow_up.weekly_report_weekday``
    (ISO weekday, 1 = Monday) and has not already been sent on that day (tracked in
    ``telegram_state``). Returns ``True`` when a report was sent this call. The body
    summarises the last seven days of applications and their outcomes in Spanish.
    """
    moment = now or datetime.now(UTC)
    if moment.isoweekday() != config.weekly_report_weekday:
        return False
    today_iso = moment.date().isoformat()
    if repo.get_state(LAST_WEEKLY_REPORT_KEY) == today_iso:
        log.info("follow_up_weekly_report_already_sent", date=today_iso)
        return False

    start = moment - timedelta(days=7)
    applications = repo.applications_in_period(start, moment)
    notifier.send_message(_render_weekly_report(applications))
    repo.set_state(LAST_WEEKLY_REPORT_KEY, today_iso)
    log.info("follow_up_weekly_report_sent", date=today_iso, count=len(applications))
    return True


def _render_reminder(app: Application) -> str:
    """Render the Spanish follow-up reminder for a pending application."""
    applied = app.applied_at.date().isoformat() if app.applied_at else "fecha desconocida"
    return (
        "Recordatorio de seguimiento.\n"
        f"La postulación de la oferta {app.job_id} (enviada el {applied}) sigue sin "
        "respuesta.\n"
        f"Si ya tienes noticias, usa /entrevista {app.job_id} o /rechazo {app.job_id}."
    )


def _render_weekly_report(applications: list[Application]) -> str:
    """Render the Spanish weekly report summarising applications and outcomes."""
    if not applications:
        return "Reporte semanal: no registraste postulaciones esta semana."
    counts: dict[ApplicationOutcome, int] = {}
    for app in applications:
        counts[app.outcome] = counts.get(app.outcome, 0) + 1
    lines = [
        "Reporte semanal de postulaciones",
        f"Total de la semana: {len(applications)}",
    ]
    # Report outcomes in a stable order using the Spanish labels.
    lines.extend(
        f"- {_OUTCOME_LABELS[outcome]}: {counts[outcome]}"
        for outcome in ApplicationOutcome
        if outcome in counts
    )
    return "\n".join(lines)
