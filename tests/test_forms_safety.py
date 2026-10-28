"""Safety behaviour of the assisted-form flow (Playwright + Notifier MOCKED).

Covers: unexpected DOM -> safe abort (screenshot, no submit); CAPTCHA -> stop +
notify, never evade; the final submit click happens ONLY on an explicit Enviar.
No real browser is launched and no Telegram call is made.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TypeVar

from pydantic import BaseModel

from jobpilot.config import AppConfig, FormsConfig, Language
from jobpilot.forms.apply import FormApplyResult, FormApplyStatus, apply_via_form
from jobpilot.forms.base import FormPage
from jobpilot.forms.greenhouse import GreenhouseFormFiller
from jobpilot.forms.submit import SubmitAction
from jobpilot.interfaces import FillContext, Update
from jobpilot.llm.schemas import FormAnswer
from jobpilot.models import ApplicationChannel, Job, JobStatus
from jobpilot.profile.model import ProfileModel
from tests.fakes import FakeFormPage, FakeJobRepository, FakeNotifier

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class _StubLlm:
    def complete_json(self, prompt: str, schema: type[SchemaT], *, max_retries: int = 1) -> SchemaT:
        answer = FormAnswer(answer="n/a", confident=True)
        assert isinstance(answer, schema)
        return answer


_GH_PRESENT = {
    "#application_form",
    "#first_name",
    "#last_name",
    "#email",
    "input#resume",
    "input#cover_letter",
    "#submit_app",
}

#: The Greenhouse confirmation marker the platform reveals after a successful submit;
#: a submit-success test's page exposes it only once the submit button is clicked.
_GH_SUCCESS = "#application_confirmation"


def _gh_success_page(**kwargs: object) -> FakeFormPage:
    """A Greenhouse page that reveals its confirmation marker on a successful click."""
    kwargs.setdefault("present", _GH_PRESENT)
    return FakeFormPage(present_after_click={_GH_SUCCESS}, **kwargs)  # type: ignore[arg-type]


# A fixed confirmation nonce so a test can predict the Enviar/Cancelar callback data.
_NONCE = "testnonce"


def _job() -> Job:
    return Job(
        id=7,
        source="greenhouse",
        external_id="e7",
        url="https://boards.greenhouse.io/acme/jobs/7",
        apply_url="https://boards.greenhouse.io/acme/jobs/7",
        title="Backend Engineer",
        company="ACME Corp",
        language=Language.en,
        status=JobStatus.approved,
    )


def _page_factory(
    page: FakeFormPage,
) -> Callable[[str, AppConfig], AbstractContextManager[FormPage]]:
    @contextmanager
    def factory(url: str, config: AppConfig) -> Iterator[FormPage]:
        yield page

    return factory


def _apply(
    profile: ProfileModel,
    page: FakeFormPage,
    notifier: FakeNotifier,
    repo: FakeJobRepository,
    *,
    cv_variant: str = "backend_en",
    cv_url: str = "https://drive.example/cv_en.pdf",
    cover_letter_url: str = "https://drive.example/cover_en.pdf",
) -> FormApplyResult:
    repo.seed_job(_job())
    filler = GreenhouseFormFiller(profile, _StubLlm(), FormsConfig())
    return apply_via_form(
        _job(),
        filler,
        notifier,
        repo,
        AppConfig(),
        language=Language.en,
        cv_path="/tmp/cv_en.pdf",
        cover_letter_path="/tmp/cover_en.pdf",
        cv_variant=cv_variant,
        cv_url=cv_url,
        cover_letter_url=cover_letter_url,
        page_factory=_page_factory(page),
        confirmation_nonce=_NONCE,
    )


def test_unexpected_dom_safe_abort_no_submit(example_profile: ProfileModel) -> None:
    # Missing the required #email marker: the structure is not understood.
    page = FakeFormPage(present={"#application_form", "#submit_app"})
    notifier = FakeNotifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert result.fill_result is not None
    assert result.fill_result.aborted is True
    assert result.fill_result.screenshot_path is not None
    assert page.clicked == []  # never submitted
    assert repo.applications == []
    # The owner was notified with the screenshot attached.
    assert any("segura" in m.text for m in notifier.messages)


def test_blank_required_field_safe_abort_no_submit(example_profile: ProfileModel) -> None:
    # #last_name exists but stays blank (a single-token name). The required-field
    # validation must abort safely before the owner is ever asked to confirm.
    page = FakeFormPage(present=_GH_PRESENT)
    notifier = FakeNotifier()
    repo = FakeJobRepository()
    stripped = example_profile.model_copy(
        update={"basics": example_profile.basics.model_copy(update={"name": "Madonna"})}
    )

    result = _apply(stripped, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert result.fill_result is not None
    assert result.fill_result.aborted is True
    assert page.clicked == []  # never submitted
    assert repo.applications == []
    assert any("segura" in m.text for m in notifier.messages)


def test_hidden_required_field_safe_abort_no_submit(example_profile: ProfileModel) -> None:
    # The email control is present but hidden, so it cannot be completed: safe abort.
    page = FakeFormPage(present=_GH_PRESENT, hidden={"#email"})
    notifier = FakeNotifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert page.clicked == []
    assert repo.applications == []


def test_captcha_stops_and_notifies_never_evades(example_profile: ProfileModel) -> None:
    page = FakeFormPage(present={*_GH_PRESENT, ".g-recaptcha"})
    notifier = FakeNotifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.captcha
    assert page.clicked == []
    assert page.filled == {}  # we stop before filling anything
    assert repo.applications == []
    assert any("CAPTCHA" in m.text for m in notifier.messages)


def test_final_click_only_on_enviar(example_profile: ProfileModel) -> None:
    page = _gh_success_page()
    # The owner presses Enviar for job 7.
    notifier = FakeNotifier(
        updates_after_send=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:7:{_NONCE}",
            )
        ]
    )
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.submitted
    assert page.clicked == ["#submit_app"]
    assert len(repo.applications) == 1
    assert repo.applications[0].channel is ApplicationChannel.form_assisted
    assert (7, JobStatus.applied) in repo.status_changes


def test_cancelar_submits_nothing(example_profile: ProfileModel) -> None:
    page = FakeFormPage(present=_GH_PRESENT)
    notifier = FakeNotifier(
        updates_after_send=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.cancel.value}:7:{_NONCE}",
            )
        ]
    )
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.cancelled
    assert page.clicked == []
    assert repo.applications == []


def test_confirmation_timeout_submits_nothing(example_profile: ProfileModel) -> None:
    from jobpilot.forms.submit import SubmitDecision, request_submit_confirmation

    page = FakeFormPage(present=_GH_PRESENT)
    # No decision update arrives.
    notifier = FakeNotifier()
    filler = GreenhouseFormFiller(example_profile, _StubLlm(), FormsConfig())

    # Drive the confirmation directly with a tiny poll budget and no sleep.
    result = filler.fill_page(page, _page_ctx())
    decision = request_submit_confirmation(
        notifier, 7, result, poll_attempts=2, poll_interval_s=0.0, sleep=lambda _: None
    )
    assert decision is SubmitDecision.timeout
    assert page.clicked == []


def test_stale_same_job_enviar_is_ignored(example_profile: ProfileModel) -> None:
    # A delayed Enviar from an earlier attempt for the SAME job carries a different
    # nonce. It must never authorize this attempt's submission.
    from jobpilot.forms.submit import SubmitDecision, request_submit_confirmation

    page = FakeFormPage(present=_GH_PRESENT)
    filler = GreenhouseFormFiller(example_profile, _StubLlm(), FormsConfig())
    result = filler.fill_page(page, _page_ctx())

    stale = Update(
        update_id=1,
        chat_id="owner",
        callback_data=f"{SubmitAction.submit.value}:7:oldnonce",
    )
    notifier = FakeNotifier(updates_after_send=[stale])

    decision = request_submit_confirmation(
        notifier, 7, result, poll_attempts=2, poll_interval_s=0.0, sleep=lambda _: None, nonce="new"
    )

    assert decision is SubmitDecision.timeout  # the stale Enviar did not count
    assert page.clicked == []


def test_only_current_attempt_enviar_submits(example_profile: ProfileModel) -> None:
    # Both a stale Enviar (old nonce) and the current attempt's Enviar are queued;
    # only the current nonce authorizes the submit.
    from jobpilot.forms.submit import SubmitDecision, request_submit_confirmation

    page = FakeFormPage(present=_GH_PRESENT)
    filler = GreenhouseFormFiller(example_profile, _StubLlm(), FormsConfig())
    result = filler.fill_page(page, _page_ctx())

    notifier = FakeNotifier(
        updates_after_send=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:7:oldnonce",
            ),
            Update(
                update_id=2,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:7:new",
            ),
        ]
    )

    decision = request_submit_confirmation(
        notifier, 7, result, poll_attempts=2, poll_interval_s=0.0, sleep=lambda _: None, nonce="new"
    )

    assert decision is SubmitDecision.submit


def test_backlog_enviar_before_prompt_is_ignored(example_profile: ProfileModel) -> None:
    # An Enviar already pending BEFORE the prompt was sent (e.g. from a crashed
    # attempt) must not authorize this submission even if the nonce happened to match.
    from jobpilot.forms.submit import SubmitDecision, request_submit_confirmation

    page = FakeFormPage(present=_GH_PRESENT)
    filler = GreenhouseFormFiller(example_profile, _StubLlm(), FormsConfig())
    result = filler.fill_page(page, _page_ctx())

    # Pre-existing (backlog) update visible immediately, before any send.
    notifier = FakeNotifier(
        updates=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:7:new",
            )
        ]
    )

    decision = request_submit_confirmation(
        notifier, 7, result, poll_attempts=2, poll_interval_s=0.0, sleep=lambda _: None, nonce="new"
    )

    assert decision is SubmitDecision.timeout
    assert page.clicked == []


def _page_ctx() -> FillContext:
    return FillContext(
        job_id=7,
        language=Language.en.value,
        cv_path="/tmp/cv_en.pdf",
        cover_letter_path="/tmp/cover_en.pdf",
    )


def test_unsupported_url_rejected_before_browser(example_profile: ProfileModel) -> None:
    page = FakeFormPage(present=_GH_PRESENT)
    notifier = FakeNotifier()
    repo = FakeJobRepository()
    filler = GreenhouseFormFiller(example_profile, _StubLlm(), FormsConfig())
    job = _job().model_copy(update={"apply_url": "https://www.linkedin.com/jobs/view/7"})

    result = apply_via_form(
        job,
        filler,
        notifier,
        repo,
        AppConfig(),
        language=Language.en,
        cv_path="/tmp/cv_en.pdf",
        cover_letter_path="/tmp/cover_en.pdf",
        cv_variant="backend_en",
        cv_url="https://drive.example/cv_en.pdf",
        cover_letter_url="https://drive.example/cover_en.pdf",
        page_factory=_page_factory(page),
    )

    assert result.status is FormApplyStatus.unsupported
    assert page.filled == {}  # browser page was never driven
    assert repo.applications == []


# --------------------------------------------------------------------------- #
# P8-007: required non-text controls and mandatory uploads gate the submit
# --------------------------------------------------------------------------- #
def test_required_select_question_safe_abort(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormQuestion

    # A required <select> the driver cannot answer must abort the form, never submit.
    question = FakeFormQuestion(
        label="How did you hear about us?",
        container_selector=".field.custom-question",
        control_id="q_source",
        kind="select",
        required=True,
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])
    notifier = FakeNotifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert page.clicked == []
    assert repo.applications == []
    assert any("segura" in m.text for m in notifier.messages)


def test_required_radio_question_safe_abort(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormQuestion

    question = FakeFormQuestion(
        label="Do you agree to the terms?",
        container_selector=".field.custom-question",
        control_id="q_terms",
        kind="radio",
        required=True,
        checked=False,
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])
    notifier = FakeNotifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert page.clicked == []
    assert repo.applications == []


def test_required_number_question_safe_abort(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormQuestion

    # A required number input (e.g. a sensitive years-in-tech left blank) aborts.
    question = FakeFormQuestion(
        label="How many years of experience do you have with SAP?",
        container_selector=".field.custom-question",
        control_id="q_years",
        kind="number",
        required=True,
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])
    notifier = FakeNotifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert page.clicked == []
    assert repo.applications == []


def test_checked_required_checkbox_allows_submit(example_profile: ProfileModel) -> None:
    from jobpilot.forms.submit import SubmitAction
    from tests.fakes import FakeFormQuestion

    # A required checkbox that is already checked is complete, so the form proceeds.
    question = FakeFormQuestion(
        label="I confirm the information is accurate.",
        container_selector=".field.custom-question",
        control_id="q_confirm",
        kind="checkbox",
        required=True,
        checked=True,
    )
    page = _gh_success_page(questions=[question])
    notifier = FakeNotifier(
        updates_after_send=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:7:{_NONCE}",
            )
        ]
    )
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.submitted
    assert page.clicked == ["#submit_app"]


def test_missing_cv_control_safe_abort(example_profile: ProfileModel) -> None:
    # The CV upload control is absent: a supported form must expose one, so abort.
    present = set(_GH_PRESENT)
    present.discard("input#resume")
    page = FakeFormPage(present=present)
    notifier = FakeNotifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert page.clicked == []
    assert repo.applications == []


def test_missing_cover_letter_control_safe_abort_on_greenhouse(
    example_profile: ProfileModel,
) -> None:
    # Greenhouse exposes a cover-letter upload, so a missing one aborts.
    present = set(_GH_PRESENT)
    present.discard("input#cover_letter")
    page = FakeFormPage(present=present)
    notifier = FakeNotifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert page.clicked == []
    assert repo.applications == []


# --------------------------------------------------------------------------- #
# P8-008: CAPTCHA / DOM change are re-checked at the final-submit boundary
# --------------------------------------------------------------------------- #
class _CaptchaOnSubmitPage(FakeFormPage):
    """A page that is clean during filling but grows a CAPTCHA before the submit."""

    def __init__(self, captcha_marker: str) -> None:
        super().__init__(present=_GH_PRESENT)
        self._captcha_marker = captcha_marker
        self._armed = False

    def screenshot(self, *, path: str) -> None:  # the fill screenshot arms the CAPTCHA
        super().screenshot(path=path)
        self._armed = True
        self._present.add(self._captcha_marker)


def test_captcha_appearing_before_submit_stops_and_notifies(
    example_profile: ProfileModel,
) -> None:
    from jobpilot.forms.submit import SubmitAction

    page = _CaptchaOnSubmitPage(".h-captcha")  # hCaptcha marker
    notifier = FakeNotifier(
        updates_after_send=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:7:{_NONCE}",
            )
        ]
    )
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    # The owner pressed Enviar, but a CAPTCHA appeared at the boundary: stop + notify,
    # never click, never record.
    assert result.status is FormApplyStatus.captcha
    assert page.clicked == []
    assert repo.applications == []
    assert any("CAPTCHA" in m.text for m in notifier.messages)


class _DomChangesBeforeSubmitPage(FakeFormPage):
    """A page whose required #email vanishes after filling (DOM re-render)."""

    def __init__(self) -> None:
        super().__init__(present=_GH_PRESENT)

    def screenshot(self, *, path: str) -> None:
        super().screenshot(path=path)
        self._present.discard("#email")  # the structure changes after confirmation


def test_dom_change_before_submit_aborts_safely(example_profile: ProfileModel) -> None:
    from jobpilot.forms.submit import SubmitAction

    page = _DomChangesBeforeSubmitPage()
    notifier = FakeNotifier(
        updates_after_send=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:7:{_NONCE}",
            )
        ]
    )
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert page.clicked == []
    assert repo.applications == []


def test_hcaptcha_detected_at_fill(example_profile: ProfileModel) -> None:
    # hCaptcha (and Cloudflare Turnstile) markers are recognised like reCAPTCHA.
    for marker in (".h-captcha", ".cf-turnstile"):
        page = FakeFormPage(present={*_GH_PRESENT, marker})
        notifier = FakeNotifier()
        repo = FakeJobRepository()
        result = _apply(example_profile, page, notifier, repo)
        assert result.status is FormApplyStatus.captcha
        assert page.clicked == []


# --------------------------------------------------------------------------- #
# P8-009: a confirmed submit records the CV variant and both document URLs
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# P8-007: the final gate verifies files remain ATTACHED, not just the selectors
# --------------------------------------------------------------------------- #
class _FilesClearedBeforeSubmitPage(FakeFormPage):
    """A page whose uploaded files are cleared after the preview (DOM re-render).

    The upload selectors stay present, so a selector-only gate would still click.
    The attachment-state gate must notice the file inputs are now empty and abort.
    """

    def __init__(self) -> None:
        super().__init__(present=_GH_PRESENT, present_after_click={_GH_SUCCESS})

    def screenshot(self, *, path: str) -> None:
        super().screenshot(path=path)
        self.uploaded.clear()  # the file inputs lose their attachments


def test_cleared_file_inputs_before_submit_abort_no_submit(
    example_profile: ProfileModel,
) -> None:
    page = _FilesClearedBeforeSubmitPage()
    notifier = FakeNotifier(
        updates_after_send=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:7:{_NONCE}",
            )
        ]
    )
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    # Both document selectors are still present, but the files were cleared during
    # the Telegram review, so the submit gate aborts: screenshot + notify, no click.
    assert result.status is FormApplyStatus.aborted
    assert page.clicked == []
    assert repo.applications == []
    assert page.screenshots  # a safe-abort screenshot was taken
    assert any("segura" in m.text for m in notifier.messages)


# --------------------------------------------------------------------------- #
# P8-008: click-time, post-click and persistence failures are safely reconciled
# --------------------------------------------------------------------------- #
def _enviar_notifier() -> FakeNotifier:
    return FakeNotifier(
        updates_after_send=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:7:{_NONCE}",
            )
        ]
    )


def test_click_timeout_is_safe_abort_not_crash(example_profile: ProfileModel) -> None:
    # A Playwright click failure (timeout / detached control) must not escape as an
    # unhandled crash: it becomes a safe abort with a screenshot and notification.
    page = FakeFormPage(present=_GH_PRESENT, click_raises=RuntimeError("click timeout"))
    notifier = _enviar_notifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert repo.applications == []
    # The click was attempted but failed; nothing was recorded and the claim released.
    assert repo.form_claim_state(7) is None
    assert any("segura" in m.text for m in notifier.messages)


class _NoSuccessMarkerPage(FakeFormPage):
    """A page that clicks successfully but never reveals a confirmation marker."""

    def __init__(self) -> None:
        super().__init__(present=_GH_PRESENT)  # no present_after_click


def test_post_click_unconfirmed_is_ambiguous_and_blocks_retry(
    example_profile: ProfileModel,
) -> None:
    # The click returns but no success marker appears: the outcome is AMBIGUOUS. The
    # claim stays in place so a retry cannot resubmit, and nothing is recorded.
    page = _NoSuccessMarkerPage()
    notifier = _enviar_notifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.ambiguous
    assert repo.applications == []
    assert repo.form_claim_state(7) == "in_progress"  # claim retained for reconciliation


class _ErrorBannerPage(FakeFormPage):
    """A page that reveals a validation error banner after the click (rejected)."""

    def __init__(self) -> None:
        super().__init__(present=_GH_PRESENT, present_after_click={"#error_box"})


def test_post_click_validation_error_is_safe_abort(example_profile: ProfileModel) -> None:
    page = _ErrorBannerPage()
    notifier = _enviar_notifier()
    repo = FakeJobRepository()

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.aborted
    assert repo.applications == []
    assert repo.form_claim_state(7) is None  # rejection releases the claim for retry


def test_database_finalize_failure_leaves_claim_and_no_record(
    example_profile: ProfileModel,
) -> None:
    # The platform accepted the submission, but recording it fails. Nothing is
    # recorded and the claim stays in place so a later Enviar cannot resubmit.
    page = _gh_success_page()
    notifier = _enviar_notifier()
    repo = FakeJobRepository()
    repo.fail_finalize = True

    result = _apply(example_profile, page, notifier, repo)

    assert result.status is FormApplyStatus.ambiguous
    assert repo.applications == []
    assert repo.form_claim_state(7) == "in_progress"
    assert any("base de datos" in m.text for m in notifier.messages)


def test_repeat_invocation_after_claim_does_not_resubmit(
    example_profile: ProfileModel,
) -> None:
    # A durable claim already exists (a prior attempt in progress/done): a second
    # confirmed Enviar must submit nothing and record nothing.
    page = _gh_success_page()
    notifier = _enviar_notifier()
    repo = FakeJobRepository()
    repo.seed_job(_job())
    repo.claim_form_submit(7)  # a prior attempt already claimed the submit

    filler = GreenhouseFormFiller(example_profile, _StubLlm(), FormsConfig())
    result = apply_via_form(
        _job(),
        filler,
        notifier,
        repo,
        AppConfig(),
        language=Language.en,
        cv_path="/tmp/cv_en.pdf",
        cover_letter_path="/tmp/cover_en.pdf",
        cv_variant="backend_en",
        cv_url="https://drive.example/cv_en.pdf",
        cover_letter_url="https://drive.example/cover_en.pdf",
        page_factory=_page_factory(page),
        confirmation_nonce=_NONCE,
    )

    assert result.status is FormApplyStatus.already_claimed
    assert page.clicked == []
    assert repo.applications == []


def test_confirmed_submit_records_document_metadata(example_profile: ProfileModel) -> None:
    from jobpilot.forms.submit import SubmitAction

    page = _gh_success_page()
    notifier = FakeNotifier(
        updates_after_send=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:7:{_NONCE}",
            )
        ]
    )
    repo = FakeJobRepository()

    result = _apply(
        example_profile,
        page,
        notifier,
        repo,
        cv_variant="backend_en",
        cv_url="https://drive.example/cv_en.pdf",
        cover_letter_url="https://drive.example/cover_en.pdf",
    )

    assert result.status is FormApplyStatus.submitted
    assert len(repo.applications) == 1
    app = repo.applications[0]
    assert app.channel is ApplicationChannel.form_assisted
    assert app.cv_variant == "backend_en"
    assert str(app.cv_url) == "https://drive.example/cv_en.pdf"
    assert str(app.cover_letter_url) == "https://drive.example/cover_en.pdf"
