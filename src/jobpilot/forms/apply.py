"""Local-only assisted-form application flow (fill -> confirm -> submit -> record).

This is the entrypoint a LOCAL run uses to apply through a supported ATS form. It
ties the pieces together with the project's human-in-the-loop guarantee intact:

#. open the form once and fill it with :meth:`FormFiller.fill_with_safe_abort`;
#. if the filler aborted (unexpected DOM) or raised a CAPTCHA stop, notify the owner
   with the saved screenshot and submit NOTHING;
#. otherwise ask the owner to confirm via Telegram (screenshot + uncertain fields +
   Enviar/Cancelar); only an explicit Enviar clicks the final submit button;
#. on a confirmed submit, record the application with channel ``form_assisted``.

It is never invoked from CI — the pipeline exposes it only for a local operator —
and every dependency (the page factory, notifier, repository, clock) is injected so
the tests drive the entire flow with a fake page and fake notifier, no browser and
no Telegram.

Important safety invariants, all enforced here:

* A CAPTCHA / anti-bot challenge stops the flow and notifies; it is never evaded.
* An unexpected form structure aborts safely (screenshot + notify), never submitting
  an incomplete form.
* The final submit click happens ONLY after an explicit Enviar; a Cancelar or a
  confirmation timeout submits nothing.
* LinkedIn Easy Apply and Indeed are out of scope and rejected before any browser
  opens (``detect_platform`` returns ``None`` for them).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from jobpilot.forms.base import (
    CaptchaError,
    FormFiller,
    FormStructureError,
    abort_screenshot,
    detect_platform,
)
from jobpilot.forms.runtime import open_form_page
from jobpilot.forms.submit import SubmitDecision, request_submit_confirmation
from jobpilot.interfaces import FillContext, FillResult
from jobpilot.logging import get_logger
from jobpilot.models import Application, ApplicationChannel, Attachment
from jobpilot.pipeline.follow_up import compute_follow_up_at

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from jobpilot.config import AppConfig, Language
    from jobpilot.forms.base import FormPage
    from jobpilot.interfaces import Notifier
    from jobpilot.models import Job

log = get_logger(__name__)


class FormApplyStatus(StrEnum):
    """What happened when applying through a form."""

    unsupported = "unsupported"
    captcha = "captcha"
    aborted = "aborted"
    cancelled = "cancelled"
    submitted = "submitted"


@dataclass(slots=True)
class FormApplyResult:
    """The outcome of a single assisted-form application attempt."""

    status: FormApplyStatus
    platform: str | None = None
    fill_result: FillResult | None = None
    application: Application | None = None


# The repository surface the form flow needs: just enough to record the application.
if TYPE_CHECKING:
    from typing import Protocol

    from jobpilot.models import JobStatus

    class FormRepository(Protocol):
        def create_application(self, app: Application) -> Application: ...

        def set_status(self, job_id: int, status: JobStatus) -> None: ...


@contextmanager
def _default_page(url: str, config: AppConfig) -> Iterator[FormPage]:
    """Default page factory: open a real Playwright page (local only)."""
    with open_form_page(url, config.forms) as page:
        yield page


def apply_via_form(
    job: Job,
    filler: FormFiller,
    notifier: Notifier,
    repo: FormRepository,
    config: AppConfig,
    *,
    language: Language,
    cv_path: str,
    cover_letter_path: str,
    page_factory: Callable[[str, AppConfig], AbstractContextManager[FormPage]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> FormApplyResult:
    """Apply to ``job`` through its supported form, with the pre-submit HITL gate.

    ``page_factory`` is a context manager yielding a :class:`FormPage`; it defaults
    to the real Playwright runtime but tests pass a factory yielding a fake page, so
    no browser is launched. The apply URL is validated with :func:`detect_platform`
    first, so LinkedIn Easy Apply / Indeed / any unknown form returns
    :attr:`FormApplyStatus.unsupported` before a browser opens.
    """
    if job.id is None:
        raise ValueError("apply_via_form requires a persisted job with an id")
    url = job.apply_url or job.url
    platform = detect_platform(url)
    if platform is None:
        log.info("form_apply_unsupported", job_id=job.id, url=url)
        notifier.send_message(
            f"La oferta {job.id} no usa un formulario compatible (o es LinkedIn/Indeed); "
            "aplícala manualmente."
        )
        return FormApplyResult(status=FormApplyStatus.unsupported)

    ctx = FillContext(
        job_id=job.id,
        language=language.value,
        cv_path=cv_path,
        cover_letter_path=cover_letter_path,
    )
    factory = page_factory or _default_page
    with factory(url, config) as page_obj:
        try:
            result = filler.fill_page(page_obj, ctx)
        except CaptchaError as exc:
            # CAPTCHA / anti-bot: stop and notify; never evade. A dedicated status so
            # the owner is told to take over, distinct from an unexpected-DOM abort.
            return _handle_captcha(job, notifier, page_obj, filler, exc)
        except FormStructureError as exc:
            # Unexpected form structure: safe abort with a screenshot, submit nothing.
            return _handle_abort(job, notifier, page_obj, filler, exc)

        decision = request_submit_confirmation(notifier, job.id, result)
        if decision is not SubmitDecision.submit:
            return _handle_not_submitted(job, notifier, result, decision)

        filler.submit(page_obj)

    application = _record(job, repo, config, language, result, now)
    notifier.send_message(f"Postulación enviada por formulario ({job.company} — {job.title}).")
    log.info("form_apply_submitted", job_id=job.id, platform=platform)
    return FormApplyResult(
        status=FormApplyStatus.submitted,
        platform=platform,
        fill_result=result,
        application=application,
    )


def _handle_captcha(
    job: Job, notifier: Notifier, page: FormPage, filler: FormFiller, exc: CaptchaError
) -> FormApplyResult:
    """Stop and notify on a CAPTCHA; the challenge is never evaded."""
    assert job.id is not None
    screenshot = abort_screenshot(page, job.id, filler.platform.value)
    log.warning("form_apply_captcha", job_id=job.id, error=str(exc))
    notifier.send_message(
        f"La oferta {job.id} muestra un CAPTCHA o verificación anti-bot. Me detengo y no la "
        "envío; complétala tú manualmente.",
        attachments=_screenshot_at(screenshot),
    )
    return FormApplyResult(status=FormApplyStatus.captcha, platform=None)


def _handle_abort(
    job: Job, notifier: Notifier, page: FormPage, filler: FormFiller, exc: FormStructureError
) -> FormApplyResult:
    """Notify on a safe abort (unexpected DOM) and submit nothing."""
    assert job.id is not None
    screenshot = abort_screenshot(page, job.id, filler.platform.value)
    result = FillResult(
        filled_fields=[],
        uncertain_fields=[],
        unanswered_sensitive=[],
        screenshot_path=screenshot,
        aborted=True,
        abort_reason=f"unexpected_structure: {exc}",
    )
    log.warning("form_apply_aborted", job_id=job.id, reason=result.abort_reason)
    notifier.send_message(
        f"No pude completar el formulario de la oferta {job.id} de forma segura "
        f"({result.abort_reason}). No envié nada; revisa la captura.",
        attachments=_screenshot_at(screenshot),
    )
    return FormApplyResult(status=FormApplyStatus.aborted, fill_result=result)


def _handle_not_submitted(
    job: Job, notifier: Notifier, result: FillResult, decision: SubmitDecision
) -> FormApplyResult:
    """Handle a Cancelar or a confirmation timeout: nothing is submitted."""
    if decision is SubmitDecision.timeout:
        notifier.send_message(
            f"No recibí confirmación para la oferta {job.id}; no la envié. Puedes reintentar."
        )
    else:
        notifier.send_message(f"Envío cancelado para la oferta {job.id}. No se postuló nada.")
    log.info("form_apply_not_submitted", job_id=job.id, decision=decision.value)
    return FormApplyResult(status=FormApplyStatus.cancelled, fill_result=result)


def _screenshot_at(path: str | None) -> list[Attachment]:
    """Load a screenshot path as a PNG attachment, if present."""
    from jobpilot.forms.submit import screenshot_attachment  # noqa: PLC0415 - reuse loader

    return screenshot_attachment(path)


def _record(
    job: Job,
    repo: FormRepository,
    config: AppConfig,
    language: Language,
    result: FillResult,
    now: Callable[[], datetime] | None,
) -> Application:
    """Record the submitted application (channel ``form_assisted``) and mark applied."""
    from jobpilot.models import JobStatus  # noqa: PLC0415 - avoid a module-load cycle

    assert job.id is not None
    applied_at = (now or (lambda: datetime.now(UTC)))()
    application = Application(
        job_id=job.id,
        language=language,
        cv_variant="",
        channel=ApplicationChannel.form_assisted,
        applied_at=applied_at,
        follow_up_at=compute_follow_up_at(applied_at, config.follow_up),
        notes="; ".join(result.uncertain_fields) or None,
    )
    stored = repo.create_application(application)
    repo.set_status(job.id, JobStatus.applied)
    log.info("form_application_recorded", job_id=job.id)
    return stored
