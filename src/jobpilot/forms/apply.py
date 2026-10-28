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
    SubmitAmbiguousError,
    abort_screenshot,
    detect_platform,
    is_unsupported_url,
)
from jobpilot.forms.runtime import open_form_page
from jobpilot.forms.submit import SubmitDecision, request_submit_confirmation
from jobpilot.interfaces import FillContext, FillResult
from jobpilot.logging import get_logger
from jobpilot.models import Application, ApplicationChannel, Attachment, JobStatus
from jobpilot.pipeline.follow_up import compute_follow_up_at

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from jobpilot.config import AppConfig, Language
    from jobpilot.forms.base import FormPage
    from jobpilot.interfaces import Notifier
    from jobpilot.models import Job

log = get_logger(__name__)

#: Job statuses from which an assisted-form application may start. Only an approved
#: job is eligible; an already applied/discarded one is refused so a stale invocation
#: cannot resubmit (P8-008).
_ELIGIBLE_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.approved})


class FormApplyStatus(StrEnum):
    """What happened when applying through a form."""

    unsupported = "unsupported"
    captcha = "captcha"
    aborted = "aborted"
    cancelled = "cancelled"
    submitted = "submitted"
    #: The owner already applied (or an attempt is in progress/ambiguous): a durable
    #: claim exists, so this invocation submits nothing and records nothing.
    already_claimed = "already_claimed"
    #: The click returned but the platform's acceptance could not be confirmed; the
    #: claim is left in place for manual reconciliation and nothing is recorded.
    ambiguous = "ambiguous"


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

    class FormRepository(Protocol):
        def get_job(self, job_id: int) -> Job | None: ...

        def set_status(self, job_id: int, status: JobStatus) -> None: ...

        def claim_form_submit(self, job_id: int) -> bool: ...

        def release_form_submit(self, job_id: int) -> None: ...

        def finalize_form_submit(self, job_id: int, app: Application) -> Application: ...


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
    cv_variant: str,
    cv_url: str,
    cover_letter_url: str,
    cover_letter_text: str | None = None,
    page_factory: Callable[[str, AppConfig], AbstractContextManager[FormPage]] | None = None,
    now: Callable[[], datetime] | None = None,
    confirmation_nonce: str | None = None,
) -> FormApplyResult:
    """Apply to ``job`` through its supported form, with the pre-submit HITL gate.

    ``page_factory`` is a context manager yielding a :class:`FormPage`; it defaults
    to the real Playwright runtime but tests pass a factory yielding a fake page, so
    no browser is launched. LinkedIn Easy Apply and Indeed are rejected before any
    browser opens. When the URL alone identifies a supported platform the flow
    dispatches directly; otherwise the page is opened and
    ``detect_platform(url, page.content())`` identifies an embedded Greenhouse/Lever
    form, returning :attr:`FormApplyStatus.unsupported` for anything still unknown.

    ``cv_variant``, ``cv_url`` and ``cover_letter_url`` are the approved proposal's
    document metadata and are REQUIRED: a form submission must carry the same tracking
    fields an email send does (R12.6), so missing metadata is rejected before any
    browser opens rather than recording an incomplete application. ``cover_letter_text``
    is the approved cover letter's plain-text body, used by a platform with no
    dedicated cover-letter upload (Lever) to place the letter in its additional
    -information control — never a letter regenerated by the LLM.

    The final submit is idempotent and reconciled: a durable per-job claim is taken
    before the click, the platform's post-click acceptance is verified, and the
    application record plus the job's ``applied`` status are finalized atomically. A
    second confirmed Enviar for the same job is refused; an unconfirmable click is
    left claimed for manual reconciliation and records nothing (P8-008).
    """
    if job.id is None:
        raise ValueError("apply_via_form requires a persisted job with an id")
    # R12.6: a form submission must carry the CV variant and both document URLs, so
    # reject missing metadata before opening or submitting anything.
    missing_meta = [
        name
        for name, value in (
            ("cv_variant", cv_variant),
            ("cv_url", cv_url),
            ("cover_letter_url", cover_letter_url),
        )
        if not value
    ]
    if missing_meta:
        raise ValueError(f"apply_via_form requires document metadata: {missing_meta}")

    # Eligibility: only an approved job may enter the assisted-form flow. An already
    # applied/discarded job is refused so a stale invocation cannot resubmit.
    persisted = repo.get_job(job.id)
    if persisted is not None and persisted.status not in _ELIGIBLE_STATUSES:
        log.info("form_apply_not_eligible", job_id=job.id, status=persisted.status.value)
        notifier.send_message(
            f"La oferta {job.id} no está en un estado apto para postular por formulario "
            f"({persisted.status.value}); no hago nada."
        )
        return FormApplyResult(status=FormApplyStatus.already_claimed, platform=None)
    url = job.apply_url or job.url

    # LinkedIn Easy Apply and Indeed are out of scope by policy and must be rejected
    # before any browser opens, even if their DOM later looked like a supported ATS.
    if is_unsupported_url(url):
        log.info("form_apply_unsupported", job_id=job.id, url=url, reason="linkedin_or_indeed")
        notifier.send_message(
            f"La oferta {job.id} es de LinkedIn/Indeed (fuera de alcance); aplícala manualmente."
        )
        return FormApplyResult(status=FormApplyStatus.unsupported)

    # A URL match is enough to dispatch; otherwise open the page and let the DOM
    # identify an embedded Greenhouse/Lever form on a company careers domain.
    url_platform = detect_platform(url)
    if url_platform is not None and url_platform != filler.platform.value:
        log.info(
            "form_apply_platform_mismatch",
            job_id=job.id,
            url_platform=url_platform,
            filler=filler.platform.value,
        )
        notifier.send_message(
            f"La oferta {job.id} usa {url_platform}, pero el llenador es "
            f"{filler.platform.value}; aplícala manualmente."
        )
        return FormApplyResult(status=FormApplyStatus.unsupported)

    ctx = FillContext(
        job_id=job.id,
        language=language.value,
        cv_path=cv_path,
        cover_letter_path=cover_letter_path,
        cover_letter_text=cover_letter_text,
    )
    factory = page_factory or _default_page
    with factory(url, config) as page_obj:
        if url_platform is None:
            # DOM-only detection: inspect the rendered page before touching anything.
            dom_platform = detect_platform(url, page_obj.content())
            if dom_platform is None:
                log.info("form_apply_unsupported", job_id=job.id, url=url, reason="unknown_dom")
                notifier.send_message(
                    f"La oferta {job.id} no usa un formulario compatible; aplícala manualmente."
                )
                return FormApplyResult(status=FormApplyStatus.unsupported)
            if dom_platform != filler.platform.value:
                log.info(
                    "form_apply_platform_mismatch",
                    job_id=job.id,
                    dom_platform=dom_platform,
                    filler=filler.platform.value,
                )
                notifier.send_message(
                    f"La oferta {job.id} usa {dom_platform}, pero el llenador es "
                    f"{filler.platform.value}; aplícala manualmente."
                )
                return FormApplyResult(status=FormApplyStatus.unsupported)
            platform = dom_platform
        else:
            platform = url_platform
        try:
            result = filler.fill_page(page_obj, ctx)
        except CaptchaError as exc:
            # CAPTCHA / anti-bot: stop and notify; never evade. A dedicated status so
            # the owner is told to take over, distinct from an unexpected-DOM abort.
            return _handle_captcha(job, notifier, page_obj, filler, exc)
        except FormStructureError as exc:
            # Unexpected form structure: safe abort with a screenshot, submit nothing.
            return _handle_abort(job, notifier, page_obj, filler, exc)

        decision = request_submit_confirmation(notifier, job.id, result, nonce=confirmation_nonce)
        if decision is not SubmitDecision.submit:
            return _handle_not_submitted(job, notifier, result, decision)

        # Durable, unique claim BEFORE the click so a second confirmed Enviar for the
        # same job can never submit again (P8-008). An existing claim means an attempt
        # is in progress, ambiguous, or already done: submit nothing.
        if not repo.claim_form_submit(job.id):
            log.info("form_apply_already_claimed", job_id=job.id)
            notifier.send_message(
                f"La oferta {job.id} ya tiene un envío por formulario en curso o completado; "
                "no la vuelvo a enviar."
            )
            return FormApplyResult(status=FormApplyStatus.already_claimed, platform=platform)

        # Final-submit boundary. A CAPTCHA may have appeared during the Telegram
        # review, the DOM / a required control may have changed, the click itself may
        # fail, or the platform may not confirm acceptance. ``filler.submit`` rechecks
        # the gate, wraps the click, and verifies the post-click state. A clean stop
        # (CAPTCHA / unexpected DOM / rejection) releases the claim so a later attempt
        # can retry; an AMBIGUOUS outcome keeps the claim for manual reconciliation.
        # NOTHING is recorded on any non-success path (R15.6/R15.7).
        try:
            filler.submit(page_obj, ctx)
        except CaptchaError as exc:
            repo.release_form_submit(job.id)
            return _handle_captcha(job, notifier, page_obj, filler, exc)
        except FormStructureError as exc:
            repo.release_form_submit(job.id)
            return _handle_abort(job, notifier, page_obj, filler, exc)
        except SubmitAmbiguousError as exc:
            # The click returned but acceptance could not be confirmed. Do NOT release
            # the claim: the remote site may already have accepted it, so block any
            # resubmission and ask the owner to reconcile it manually.
            return _handle_ambiguous(job, notifier, page_obj, filler, exc)

    try:
        application = _finalize(
            job,
            repo,
            config,
            language,
            result,
            now,
            cv_variant=cv_variant,
            cv_url=cv_url,
            cover_letter_url=cover_letter_url,
        )
    except Exception as exc:  # noqa: BLE001 - a post-accept DB failure is ambiguous
        # The platform accepted the submission but recording it failed. The claim
        # stays ``in_progress`` (never released), so a later Enviar cannot resubmit;
        # the owner is told to reconcile the record manually (P8-008).
        log.error("form_apply_finalize_failed", job_id=job.id, error=str(exc))
        notifier.send_message(
            f"Envié la oferta {job.id} por formulario, pero no pude guardar el registro "
            "en la base de datos. No reintentaré el envío; revisa y regístralo tú manualmente."
        )
        return FormApplyResult(
            status=FormApplyStatus.ambiguous, platform=platform, fill_result=result
        )

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


def _handle_ambiguous(
    job: Job, notifier: Notifier, page: FormPage, filler: FormFiller, exc: SubmitAmbiguousError
) -> FormApplyResult:
    """Handle an unconfirmable submit: keep the claim, record nothing, notify."""
    assert job.id is not None
    screenshot = abort_screenshot(page, job.id, filler.platform.value)
    log.warning("form_apply_ambiguous", job_id=job.id, error=str(exc))
    notifier.send_message(
        f"Pulsé enviar en la oferta {job.id}, pero no pude confirmar si el formulario se "
        "aceptó. No la reintentaré para no duplicar; revisa el estado y regístrala tú "
        "manualmente.",
        attachments=_screenshot_at(screenshot),
    )
    return FormApplyResult(status=FormApplyStatus.ambiguous, platform=filler.platform.value)


def _screenshot_at(path: str | None) -> list[Attachment]:
    """Load a screenshot path as a PNG attachment, if present."""
    from jobpilot.forms.submit import screenshot_attachment  # noqa: PLC0415 - reuse loader

    return screenshot_attachment(path)


def _finalize(
    job: Job,
    repo: FormRepository,
    config: AppConfig,
    language: Language,
    result: FillResult,
    now: Callable[[], datetime] | None,
    *,
    cv_variant: str,
    cv_url: str,
    cover_letter_url: str,
) -> Application:
    """Atomically record the submitted application and mark the job applied.

    The record, the job's ``applied`` status and the form-submit claim's ``done``
    state are committed together via ``finalize_form_submit`` so a crash cannot leave
    a half-written state; a failure propagates to the caller, which keeps the claim
    ``in_progress`` (ambiguous) and never resubmits. The approved proposal's document
    metadata (CV variant and both document URLs) is persisted so a form submission
    carries the same tracking fields an email send does (R12.6).
    """
    assert job.id is not None
    applied_at = (now or (lambda: datetime.now(UTC)))()
    application = Application(
        job_id=job.id,
        language=language,
        cv_variant=cv_variant,
        cv_url=cv_url,
        cover_letter_url=cover_letter_url,
        channel=ApplicationChannel.form_assisted,
        applied_at=applied_at,
        follow_up_at=compute_follow_up_at(applied_at, config.follow_up),
        notes="; ".join(result.uncertain_fields) or None,
    )
    stored = repo.finalize_form_submit(job.id, application)
    log.info("form_application_recorded", job_id=job.id)
    return stored
