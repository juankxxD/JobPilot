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
    "#email",
    "input#resume",
    "input#cover_letter",
    "#submit_app",
}


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
) -> Callable[[str, AppConfig], AbstractContextManager[FakeFormPage]]:
    @contextmanager
    def factory(url: str, config: AppConfig) -> Iterator[FakeFormPage]:
        yield page

    return factory


def _apply(
    profile: ProfileModel,
    page: FakeFormPage,
    notifier: FakeNotifier,
    repo: FakeJobRepository,
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
        page_factory=_page_factory(page),
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
    page = FakeFormPage(present=_GH_PRESENT)
    # The owner presses Enviar for job 7.
    notifier = FakeNotifier(
        updates=[
            Update(update_id=1, chat_id="owner", callback_data=f"{SubmitAction.submit.value}:7")
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
        updates=[
            Update(update_id=1, chat_id="owner", callback_data=f"{SubmitAction.cancel.value}:7")
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
        page_factory=_page_factory(page),
    )

    assert result.status is FormApplyStatus.unsupported
    assert page.filled == {}  # browser page was never driven
    assert repo.applications == []
