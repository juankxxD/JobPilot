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
    "#last_name",
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
    from tests.fakes import FakeFormPage, FakeFormQuestion  # noqa: F401

    llm = TrackingLlm()
    filler = GreenhouseFormFiller(example_profile, llm, FormsConfig())
    page = FakeFormPage(present=_GH_PRESENT)

    result = filler.fill_page(page, _ctx(example_profile))

    # The owner-approved full name is split into first/last across the two controls.
    first, _, last = example_profile.basics.name.partition(" ")
    assert page.filled["#first_name"] == first
    assert page.filled["#last_name"] == last
    assert page.filled["#email"] == str(example_profile.basics.email)
    assert page.filled["#phone"] == example_profile.basics.phone
    assert "first_name" in result.filled_fields
    assert "last_name" in result.filled_fields
    assert "email" in result.filled_fields
    assert not result.aborted


def test_cv_and_cover_uploaded_in_chosen_language(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormPage, FakeFormQuestion  # noqa: F401

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
    from tests.fakes import FakeFormPage, FakeFormQuestion  # noqa: F401

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
    question = FakeFormQuestion(
        label="What is your expected salary for this role?",
        container_selector=".field.custom-question",
        control_id="q_salary",
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(stripped))

    # The LLM was NEVER asked the salary question.
    assert all("salary" not in p.lower() for p in llm.prompts)
    assert "#q_salary" not in page.filled
    assert result.unanswered_sensitive == ["What is your expected salary for this role?"]


def test_sensitive_question_answered_from_bank_not_llm(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormPage, FakeFormQuestion  # noqa: F401

    llm = TrackingLlm()
    filler = GreenhouseFormFiller(example_profile, llm, FormsConfig())
    # work_authorization is sensitive and has an approved bank entry -> used verbatim.
    question = FakeFormQuestion(
        label="Do you require visa sponsorship to work?",
        container_selector=".field.custom-question",
        control_id="q_visa",
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(example_profile))

    bank = example_profile.answer_by_key("work_authorization")
    assert bank is not None
    assert page.filled["#q_visa"] == bank.text(Language.en)
    assert all("visa" not in p.lower() for p in llm.prompts)
    assert not result.unanswered_sensitive


def test_years_in_absent_technology_never_reaches_llm(example_profile: ProfileModel) -> None:
    # R15.5: a years-of-experience question that names a technology is sensitive even
    # when that technology is absent from the profile. It must NOT reach the LLM; with
    # no approved bank answer it is left blank and reported as unanswered sensitive.
    from tests.fakes import FakeFormPage, FakeFormQuestion  # noqa: F401

    llm = TrackingLlm()
    # SAP appears nowhere in the example profile's skills/tags.
    assert "sap" not in {t.lower() for t in example_profile.skills.all()}
    filler = GreenhouseFormFiller(example_profile, llm, FormsConfig())
    question = FakeFormQuestion(
        label="How many years of experience do you have with SAP?",
        container_selector=".field.custom-question",
        control_id="q_sap_years",
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(example_profile))

    assert llm.prompts == [], "a years-in-a-technology question must never reach the LLM"
    assert "#q_sap_years" not in page.filled
    assert result.unanswered_sensitive == ["How many years of experience do you have with SAP?"]


def test_generic_years_question_is_not_sensitive(example_profile: ProfileModel) -> None:
    # A generic-tenure question names no technology, so it is answered normally.
    from jobpilot.forms.base import is_sensitive_question

    assert is_sensitive_question("How many years of total experience do you have?") is False
    assert is_sensitive_question("How many years of experience do you have with Kubernetes?")
    assert is_sensitive_question("¿Cuántos años de experiencia tienes con SAP?")


def test_non_sensitive_open_question_answered_by_llm(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormPage, FakeFormQuestion  # noqa: F401

    llm = TrackingLlm()
    # Remove the bank entries that would short-circuit an LLM answer so the open
    # question actually routes to the model.
    stripped = example_profile.model_copy(update={"answers_bank": []})
    filler = GreenhouseFormFiller(stripped, llm, FormsConfig())
    question = FakeFormQuestion(
        label="Why do you want to work here?",
        container_selector=".field.custom-question",
        control_id="q_why",
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(stripped))

    assert llm.prompts, "the LLM should answer a non-sensitive open question"
    assert page.filled["#q_why"]
    assert "Why do you want to work here?" in result.filled_fields


def test_low_confidence_answer_marked_uncertain(example_profile: ProfileModel) -> None:
    from tests.fakes import FakeFormPage, FakeFormQuestion  # noqa: F401

    llm = TrackingLlm(confident=False)
    stripped = example_profile.model_copy(update={"answers_bank": []})
    filler = GreenhouseFormFiller(stripped, llm, FormsConfig())
    question = FakeFormQuestion(
        label="Why do you want to work here?",
        container_selector=".field.custom-question",
        control_id="q_why",
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

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
    from tests.fakes import FakeFormPage, FakeFormQuestion  # noqa: F401

    filler = LeverFormFiller(example_profile, TrackingLlm(), FormsConfig())
    page = FakeFormPage(present=_LEVER_PRESENT)

    result = filler.fill_page(page, _ctx(example_profile))

    assert page.filled["input[name='name']"] == example_profile.basics.name
    assert page.uploaded["input[name='resume']"] == "/tmp/cv_en.pdf"
    assert "name" in result.filled_fields
    assert not result.aborted
    # The organization control is never filled with the candidate's location.
    assert "input[name='org']" not in page.filled
