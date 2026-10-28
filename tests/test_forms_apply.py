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
