"""Field mapping, document upload and the sensitive-question guard (Playwright MOCKED).

No real browser is launched: the fillers are driven through ``FakeFormPage``. A
tracking LLM records every question it is asked so the test can prove a sensitive
question never reaches it.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from jobpilot.config import FormsConfig, Language
from jobpilot.forms.greenhouse import GreenhouseFormFiller
from jobpilot.forms.lever import LeverFormFiller
from jobpilot.interfaces import FillContext
from jobpilot.llm.schemas import FormAnswer
from jobpilot.profile.model import ProfileModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class TrackingLlm:
    """An LlmClient that returns a canned FormAnswer and records asked questions."""

    def __init__(self, *, confident: bool = True) -> None:
        self.prompts: list[str] = []
        self._confident = confident

    def complete_json(self, prompt: str, schema: type[SchemaT], *, max_retries: int = 1) -> SchemaT:
        self.prompts.append(prompt)
        answer = FormAnswer(
            answer="Available to start with two weeks' notice.", confident=self._confident
        )
        assert isinstance(answer, schema)
        return answer


# Greenhouse DOM: the known fields present plus the required structure markers.
_GH_PRESENT = {
    "#application_form",
    "#first_name",
    "#email",
    "#phone",
    "input#resume",
    "input#cover_letter",
    "#submit_app",
}


def _ctx(profile: ProfileModel) -> FillContext:
    return FillContext(
        job_id=1,
        language=Language.en.value,
        cv_path="/tmp/cv_en.pdf",
        cover_letter_path="/tmp/cover_en.pdf",
    )


def test_known_fields_mapped_from_profile(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormPage

    llm = TrackingLlm()
    filler = GreenhouseFormFiller(example_profile, llm, FormsConfig())
    page = FakeFormPage(present=_GH_PRESENT)

    result = filler.fill_page(page, _ctx(example_profile))

    assert page.filled["#first_name"] == example_profile.basics.name
    assert page.filled["#email"] == str(example_profile.basics.email)
    assert page.filled["#phone"] == example_profile.basics.phone
    assert "name" in result.filled_fields
    assert "email" in result.filled_fields
    assert not result.aborted


def test_cv_and_cover_uploaded_in_chosen_language(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormPage

    filler = GreenhouseFormFiller(example_profile, TrackingLlm(), FormsConfig())
    page = FakeFormPage(present=_GH_PRESENT)

    result = filler.fill_page(page, _ctx(example_profile))

    assert page.uploaded["input#resume"] == "/tmp/cv_en.pdf"
    assert page.uploaded["input#cover_letter"] == "/tmp/cover_en.pdf"
    assert "cv" in result.filled_fields
    assert "cover_letter" in result.filled_fields


def test_sensitive_question_never_reaches_llm_and_is_reported(
    example_profile: ProfileModel,
) -> None:
    from tests.fakes import FakeFormPage

    llm = TrackingLlm()
    filler = GreenhouseFormFiller(example_profile, llm, FormsConfig())
    # A salary-expectation question with no matching bank entry must be left blank.
    # The example profile has a salary_expectation bank entry, so remove it first to
    # prove the "no approved answer -> unanswered_sensitive" path.
    stripped = example_profile.model_copy(
        update={
            "answers_bank": [
                e for e in example_profile.answers_bank if e.key != "salary_expectation"
            ]
        }
    )
    filler = GreenhouseFormFiller(stripped, llm, FormsConfig())
    question = ("textarea#q_salary", "What is your expected salary for this role?")
    page = FakeFormPage(present={*_GH_PRESENT, "textarea#q_salary"}, questions=[question])

    result = filler.fill_page(page, _ctx(stripped))

    # The LLM was NEVER asked the salary question.
    assert all("salary" not in p.lower() for p in llm.prompts)
    assert "textarea#q_salary" not in page.filled
    assert result.unanswered_sensitive == ["What is your expected salary for this role?"]


def test_sensitive_question_answered_from_bank_not_llm(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormPage

    llm = TrackingLlm()
    filler = GreenhouseFormFiller(example_profile, llm, FormsConfig())
    # work_authorization is sensitive and has an approved bank entry -> used verbatim.
    question = ("textarea#q_visa", "Do you require visa sponsorship to work?")
    page = FakeFormPage(present={*_GH_PRESENT, "textarea#q_visa"}, questions=[question])

    result = filler.fill_page(page, _ctx(example_profile))

    bank = example_profile.answer_by_key("work_authorization")
    assert bank is not None
    assert page.filled["textarea#q_visa"] == bank.text(Language.en)
    assert all("visa" not in p.lower() for p in llm.prompts)
    assert not result.unanswered_sensitive


def test_non_sensitive_open_question_answered_by_llm(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormPage

    llm = TrackingLlm()
    # Remove the bank entries that would short-circuit an LLM answer so the open
    # question actually routes to the model.
    stripped = example_profile.model_copy(update={"answers_bank": []})
    filler = GreenhouseFormFiller(stripped, llm, FormsConfig())
    question = ("textarea#q_why", "Why do you want to work here?")
    page = FakeFormPage(present={*_GH_PRESENT, "textarea#q_why"}, questions=[question])

    result = filler.fill_page(page, _ctx(stripped))

    assert llm.prompts, "the LLM should answer a non-sensitive open question"
    assert page.filled["textarea#q_why"]
    assert "Why do you want to work here?" in result.filled_fields


def test_low_confidence_answer_marked_uncertain(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormPage

    llm = TrackingLlm(confident=False)
    stripped = example_profile.model_copy(update={"answers_bank": []})
    filler = GreenhouseFormFiller(stripped, llm, FormsConfig())
    question = ("textarea#q_why", "Why do you want to work here?")
    page = FakeFormPage(present={*_GH_PRESENT, "textarea#q_why"}, questions=[question])

    result = filler.fill_page(page, _ctx(stripped))

    assert "Why do you want to work here?" in result.uncertain_fields


_LEVER_PRESENT = {
    ".application-form",
    "input[name='name']",
    "input[name='email']",
    "input[name='resume']",
    "button[data-qa='btn-submit']",
}


def test_lever_known_fields_mapped(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormPage

    filler = LeverFormFiller(example_profile, TrackingLlm(), FormsConfig())
    page = FakeFormPage(present=_LEVER_PRESENT)

    result = filler.fill_page(page, _ctx(example_profile))

    assert page.filled["input[name='name']"] == example_profile.basics.name
    assert page.uploaded["input[name='resume']"] == "/tmp/cv_en.pdf"
    assert "name" in result.filled_fields
    assert not result.aborted
