"""FormFiller.matches dispatch and the FakeFormFiller used by later wiring tests."""

from __future__ import annotations

from jobpilot.config import FormsConfig
from jobpilot.forms.greenhouse import GreenhouseFormFiller
from jobpilot.forms.lever import LeverFormFiller
from jobpilot.interfaces import FillContext, FillResult
from jobpilot.profile.model import ProfileModel
from tests.fakes import FakeFormFiller


class _StubLlm:
    def complete_json(self, prompt, schema, *, max_retries=1):  # type: ignore[no-untyped-def]
        raise AssertionError("matches() must not call the LLM")


def test_greenhouse_matches_only_greenhouse_urls(example_profile: ProfileModel) -> None:
    filler = GreenhouseFormFiller(example_profile, _StubLlm(), FormsConfig())
    assert filler.matches("https://boards.greenhouse.io/acme/jobs/1") is True
    assert filler.matches("https://jobs.lever.co/acme/1") is False
    assert filler.matches("https://www.linkedin.com/jobs/view/1") is False


def test_lever_matches_only_lever_urls(example_profile: ProfileModel) -> None:
    filler = LeverFormFiller(example_profile, _StubLlm(), FormsConfig())
    assert filler.matches("https://jobs.lever.co/acme/1") is True
    assert filler.matches("https://boards.greenhouse.io/acme/jobs/1") is False


def test_company_domain_dom_dispatches_to_greenhouse(example_profile: ProfileModel) -> None:
    # An embedded Greenhouse form on a company careers domain: the URL gives nothing
    # away, but the page DOM identifies the platform and the flow proceeds to submit.
    from collections.abc import Iterator
    from contextlib import contextmanager

    from jobpilot.config import AppConfig, Language
    from jobpilot.forms.apply import FormApplyStatus, apply_via_form
    from jobpilot.forms.base import FormPage
    from jobpilot.forms.submit import SubmitAction
    from jobpilot.interfaces import Update
    from jobpilot.models import Job, JobStatus
    from tests.fakes import FakeFormPage, FakeJobRepository, FakeNotifier

    gh_present = {
        "#application_form",
        "#first_name",
        "#last_name",
        "#email",
        "input#resume",
        "input#cover_letter",
        "#submit_app",
    }
    dom = '<div id="grnhse_app"></div>'
    # The confirmation marker appears only after a successful submit click.
    page = FakeFormPage(
        present=gh_present, dom=dom, present_after_click={"#application_confirmation"}
    )

    @contextmanager
    def factory(url: str, config: AppConfig) -> Iterator[FormPage]:
        yield page

    job = Job(
        id=11,
        source="careers",
        external_id="e11",
        url="https://careers.acme.com/apply",
        apply_url="https://careers.acme.com/apply",
        title="Backend Engineer",
        company="ACME Corp",
        language=Language.en,
        status=JobStatus.approved,
    )
    repo = FakeJobRepository()
    repo.seed_job(job)
    notifier = FakeNotifier(
        updates_after_send=[
            Update(update_id=1, chat_id="owner", callback_data=f"{SubmitAction.submit.value}:11:n")
        ]
    )
    filler = GreenhouseFormFiller(example_profile, _StubLlm(), FormsConfig())

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
        page_factory=factory,
        confirmation_nonce="n",
    )

    assert result.status is FormApplyStatus.submitted
    assert result.platform == "greenhouse"
    assert page.clicked == ["#submit_app"]


def test_unknown_company_domain_dom_is_unsupported(example_profile: ProfileModel) -> None:
    # A company domain whose DOM matches no supported platform is rejected safely.
    from collections.abc import Iterator
    from contextlib import contextmanager

    from jobpilot.config import AppConfig, Language
    from jobpilot.forms.apply import FormApplyStatus, apply_via_form
    from jobpilot.forms.base import FormPage
    from jobpilot.models import Job, JobStatus
    from tests.fakes import FakeFormPage, FakeJobRepository, FakeNotifier

    page = FakeFormPage(present=set(), dom="<div>nothing here</div>")

    @contextmanager
    def factory(url: str, config: AppConfig) -> Iterator[FormPage]:
        yield page

    job = Job(
        id=12,
        source="careers",
        external_id="e12",
        url="https://careers.acme.com/apply",
        apply_url="https://careers.acme.com/apply",
        title="Backend Engineer",
        company="ACME Corp",
        language=Language.en,
        status=JobStatus.approved,
    )
    repo = FakeJobRepository()
    repo.seed_job(job)
    notifier = FakeNotifier()
    filler = GreenhouseFormFiller(example_profile, _StubLlm(), FormsConfig())

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
        page_factory=factory,
    )

    assert result.status is FormApplyStatus.unsupported
    assert page.clicked == []
    assert repo.applications == []


def test_fake_form_filler_records_calls() -> None:
    result = FillResult(filled_fields=["name"], uncertain_fields=[], unanswered_sensitive=[])
    filler = FakeFormFiller(result, handles="greenhouse.io")
    ctx = FillContext(
        job_id=3, language="en", cv_path="/tmp/cv.pdf", cover_letter_path="/tmp/c.pdf"
    )

    assert filler.matches("https://boards.greenhouse.io/x") is True
    assert filler.matches("https://jobs.lever.co/x") is False
    out = filler.fill("https://boards.greenhouse.io/x", ctx)

    assert out is result
    assert filler.fill_calls == [("https://boards.greenhouse.io/x", ctx)]
