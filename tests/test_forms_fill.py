"""Field mapping, document upload and the sensitive-question guard (Playwright MOCKED).

No real browser is launched: the fillers are driven through ``FakeFormPage``. A
tracking LLM records every question it is asked so the test can prove a sensitive
question never reaches it.
"""

from __future__ import annotations

from typing import TypeVar

import pytest
from pydantic import BaseModel

from jobpilot.config import FormsConfig, Language
from jobpilot.forms.base import is_sensitive_question
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
        cover_letter_text="Dear hiring team, I am excited to apply for this role.",
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
    # Lever has no dedicated cover-letter upload; the approved cover-letter TEXT goes
    # into its "additional information" textarea, so that control must be present.
    "textarea[name='comments']",
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


def test_lever_cover_letter_text_placed_in_additional_info(
    example_profile: ProfileModel,
) -> None:
    # P8-011: with no dedicated cover-letter upload, Lever places the APPROVED cover
    # letter TEXT into its additional-information control — never a letter the LLM
    # regenerated — so the application carries the cover letter.
    from tests.fakes import FakeFormPage

    filler = LeverFormFiller(example_profile, TrackingLlm(), FormsConfig())
    page = FakeFormPage(present=_LEVER_PRESENT)

    result = filler.fill_page(page, _ctx(example_profile))

    assert (
        page.filled["textarea[name='comments']"]
        == "Dear hiring team, I am excited to apply for this role."
    )
    assert "cover_letter" in result.filled_fields
    assert not result.aborted


def test_lever_without_cover_letter_destination_safe_aborts(
    example_profile: ProfileModel,
) -> None:
    # P8-011: if Lever exposes neither a dedicated upload nor the additional-info
    # control, there is no safe destination for the cover letter, so the form aborts
    # rather than submitting a CV-only application.
    from tests.fakes import FakeFormPage

    present = set(_LEVER_PRESENT)
    present.discard("textarea[name='comments']")
    filler = LeverFormFiller(example_profile, TrackingLlm(), FormsConfig())
    page = FakeFormPage(present=present)

    result = filler.fill_with_safe_abort(page, _ctx(example_profile))

    assert result.aborted
    assert "Carta de presentación" in (result.abort_reason or "")


def test_lever_without_cover_letter_text_safe_aborts(example_profile: ProfileModel) -> None:
    # P8-011: the additional-info control exists but no approved cover-letter text was
    # supplied, so there is nothing safe to place — abort rather than submit CV-only.
    from tests.fakes import FakeFormPage

    filler = LeverFormFiller(example_profile, TrackingLlm(), FormsConfig())
    page = FakeFormPage(present=_LEVER_PRESENT)
    ctx = FillContext(
        job_id=1,
        language=Language.en.value,
        cv_path="/tmp/cv_en.pdf",
        cover_letter_path="/tmp/cover_en.pdf",
        cover_letter_text=None,
    )

    result = filler.fill_with_safe_abort(page, ctx)

    assert result.aborted


# --------------------------------------------------------------------------- #
# P8-006: every protected category is classified sensitive and never reaches the LLM
# --------------------------------------------------------------------------- #
# One representative, common-wording question per protected category (R15.5). Each
# must be classified sensitive even though its exact wording is not a bank pattern.
_PROTECTED_CATEGORY_QUESTIONS: list[tuple[str, str]] = [
    ("salary", "What is your expected salary for this role?"),
    ("salary_rate", "What is your desired pay rate?"),
    ("work_authorization", "Are you legally eligible for employment in the United States?"),
    ("work_authorization_citizen", "Are you a US citizen?"),
    ("work_authorization_sponsorship", "Will you require sponsorship now or in the future?"),
    ("relocation", "Are you willing to move to Austin for this role?"),
    ("relocation_open", "Are you open to relocation?"),
    ("legal_background", "Have you ever been convicted of a felony?"),
    ("legal_noncompete", "Are you subject to a non-compete agreement?"),
    ("demographics_gender", "What is your gender?"),
    ("demographics_pronouns", "What are your pronouns?"),
    ("demographics_veteran", "Are you a protected veteran?"),
    ("demographics_disability", "Do you have a disability?"),
    ("years_in_tech_how_many", "How many years of experience do you have with SAP?"),
    ("years_in_tech_number", "Number of years using Kubernetes"),
    ("years_in_tech_parenthetical", "Python experience (years)"),
    ("years_in_tech_state", "Please state years of Java experience"),
    ("years_in_tech_es", "¿Cuántos años de experiencia tienes con SAP?"),
    # P8-006 regression: the exact review spot-check phrasings that previously slipped
    # past the guard and reached the LLM. Each must now classify as sensitive.
    ("work_auth_lawful", "Are you lawfully permitted to work in the United States?"),
    ("relocation_moving", "Are you open to moving to New York?"),
    ("legal_arrested", "Have you ever been arrested?"),
    ("demographics_origin", "Are you of Hispanic or Latino origin?"),
    ("years_in_tech_dash", "Java - years"),
]


_PROTECTED_IDS = [c for c, _ in _PROTECTED_CATEGORY_QUESTIONS]
_PROTECTED_VALUES = [q for _, q in _PROTECTED_CATEGORY_QUESTIONS]


@pytest.mark.parametrize("question", _PROTECTED_VALUES, ids=_PROTECTED_IDS)
def test_protected_category_classified_sensitive(question: str) -> None:
    assert is_sensitive_question(question) is True


# Generic-tenure and ordinary open questions must NOT be classified sensitive, so
# the broadened classifier does not block legitimate LLM answering.
_NON_SENSITIVE_QUESTIONS = [
    "How many years of total experience do you have?",
    "How many years of professional experience?",
    "Why do you want to work here?",
    "Tell us about a challenging project you led.",
    "What is your availability or start date?",
    "What interests you about this company?",
]


@pytest.mark.parametrize("question", _NON_SENSITIVE_QUESTIONS)
def test_non_sensitive_questions_not_blocked(question: str) -> None:
    assert is_sensitive_question(question) is False


@pytest.mark.parametrize("question", _PROTECTED_VALUES, ids=_PROTECTED_IDS)
def test_protected_category_never_reaches_llm_and_is_blank_without_bank(
    example_profile: ProfileModel, question: str
) -> None:
    # With no matching approved bank answer, every protected-category question is left
    # blank and reported as unanswered sensitive — never routed to the LLM (R15.5).
    from tests.fakes import FakeFormPage, FakeFormQuestion  # noqa: F401

    stripped = example_profile.model_copy(update={"answers_bank": []})
    llm = TrackingLlm()
    filler = GreenhouseFormFiller(stripped, llm, FormsConfig())
    gh_question = FakeFormQuestion(
        label=question,
        container_selector=".field.custom-question",
        control_id="q_sensitive",
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[gh_question])

    result = filler.fill_page(page, _ctx(stripped))

    assert llm.prompts == [], f"sensitive question reached the LLM: {question!r}"
    assert "#q_sensitive" not in page.filled
    assert result.unanswered_sensitive == [question]


def test_sensitive_question_from_bank_never_reaches_llm(example_profile: ProfileModel) -> None:
    # An approved bank entry answers a sensitive question verbatim; the LLM is still
    # never consulted for it.
    from tests.fakes import FakeFormPage, FakeFormQuestion  # noqa: F401

    llm = TrackingLlm()
    filler = GreenhouseFormFiller(example_profile, llm, FormsConfig())
    question = FakeFormQuestion(
        label="Are you willing to relocate?",
        container_selector=".field.custom-question",
        control_id="q_reloc",
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(example_profile))

    bank = example_profile.answer_by_key("relocation")
    assert bank is not None
    assert page.filled["#q_reloc"] == bank.text(Language.en)
    assert llm.prompts == []
    assert not result.unanswered_sensitive


# --------------------------------------------------------------------------- #
# P8-010: non-text sensitive controls are answered only by an exact bank match,
# otherwise left blank and reported — never routed to the LLM.
# --------------------------------------------------------------------------- #
def _profile_with_bank(
    example_profile: ProfileModel, key: str, patterns: list[str], value: str
) -> ProfileModel:
    """Return a copy of the profile carrying one extra approved bank entry."""
    from jobpilot.profile.model import AnswerBankEntry

    entry = AnswerBankEntry(key=key, question_patterns=patterns, sensitive=True, es=value, en=value)
    # Only the single tailored entry is present, so no other approved pattern (e.g.
    # the example profile's broad "visa" entry) intercepts the question first.
    return example_profile.model_copy(update={"answers_bank": [entry]})


def test_sensitive_select_answered_from_exact_bank_option(example_profile: ProfileModel) -> None:
    # A work-authorization <select> whose approved bank answer exactly matches one of
    # its options is selected from the bank; the LLM is never consulted (R15.5).
    from tests.fakes import FakeFormPage, FakeFormQuestion

    profile = _profile_with_bank(
        example_profile,
        key="visa_select",
        patterns=["visa sponsorship"],
        value="No sponsorship required",
    )
    llm = TrackingLlm()
    filler = GreenhouseFormFiller(profile, llm, FormsConfig())
    question = FakeFormQuestion(
        label="Do you require visa sponsorship?",
        container_selector=".field.custom-question",
        control_id="q_visa_select",
        kind="select",
        required=True,
        options=["Yes, sponsorship required", "No sponsorship required"],
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(profile))

    assert page.filled["#q_visa_select"] == "No sponsorship required"
    assert llm.prompts == []
    assert not result.unanswered_sensitive
    assert not result.aborted


def test_optional_sensitive_select_without_bank_is_blank_and_reported(
    example_profile: ProfileModel,
) -> None:
    # An OPTIONAL sensitive <select> with no approved answer is left blank and
    # reported in unanswered_sensitive — not routed to the LLM, not aborted.
    from tests.fakes import FakeFormPage, FakeFormQuestion

    stripped = example_profile.model_copy(update={"answers_bank": []})
    llm = TrackingLlm()
    filler = GreenhouseFormFiller(stripped, llm, FormsConfig())
    question = FakeFormQuestion(
        label="What is your gender?",
        container_selector=".field.custom-question",
        control_id="q_gender",
        kind="select",
        required=False,
        options=["Male", "Female", "Prefer not to say"],
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(stripped))

    assert "#q_gender" not in page.filled
    assert llm.prompts == []
    assert result.unanswered_sensitive == ["What is your gender?"]
    assert not result.aborted


def test_sensitive_select_without_matching_option_is_blank_and_reported(
    example_profile: ProfileModel,
) -> None:
    # A bank answer that matches no offered option must NOT be force-fit; the control
    # stays blank and is reported, never guessed.
    from tests.fakes import FakeFormPage, FakeFormQuestion

    profile = _profile_with_bank(
        example_profile,
        key="visa_select",
        patterns=["visa sponsorship"],
        value="It depends on the contract",
    )
    llm = TrackingLlm()
    filler = GreenhouseFormFiller(profile, llm, FormsConfig())
    question = FakeFormQuestion(
        label="Do you require visa sponsorship?",
        container_selector=".field.custom-question",
        control_id="q_visa_select",
        kind="select",
        required=False,
        options=["Yes", "No"],
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(profile))

    assert "#q_visa_select" not in page.filled
    assert llm.prompts == []
    assert result.unanswered_sensitive == ["Do you require visa sponsorship?"]


def test_sensitive_number_answered_from_numeric_bank(example_profile: ProfileModel) -> None:
    # A years-in-tech number input with a numeric approved answer is filled from the
    # bank; the LLM is never consulted.
    from tests.fakes import FakeFormPage, FakeFormQuestion

    profile = _profile_with_bank(
        example_profile,
        key="years_sap",
        patterns=["years of experience do you have with sap"],
        value="4",
    )
    llm = TrackingLlm()
    filler = GreenhouseFormFiller(profile, llm, FormsConfig())
    question = FakeFormQuestion(
        label="How many years of experience do you have with SAP?",
        container_selector=".field.custom-question",
        control_id="q_sap_num",
        kind="number",
        required=True,
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(profile))

    assert page.filled["#q_sap_num"] == "4"
    assert llm.prompts == []
    assert not result.unanswered_sensitive
    assert not result.aborted


def test_sensitive_radio_answered_from_exact_bank_option(example_profile: ProfileModel) -> None:
    # A demographic radio whose approved answer matches the control's value is checked
    # from the bank; the LLM is never consulted.
    from tests.fakes import FakeFormPage, FakeFormQuestion

    profile = _profile_with_bank(
        example_profile,
        key="veteran_status",
        patterns=["protected veteran"],
        value="I am not a protected veteran",
    )
    llm = TrackingLlm()
    filler = GreenhouseFormFiller(profile, llm, FormsConfig())
    question = FakeFormQuestion(
        label="Are you a protected veteran?",
        container_selector=".field.custom-question",
        control_id="q_vet",
        kind="radio",
        required=True,
        options=["I am not a protected veteran", "I am a protected veteran"],
    )
    page = FakeFormPage(present=_GH_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(profile))

    assert "#q_vet" in page._checked  # the radio was checked from the bank
    assert llm.prompts == []
    assert not result.unanswered_sensitive
    assert not result.aborted


def test_lever_sensitive_select_without_bank_is_blank_and_reported(
    example_profile: ProfileModel,
) -> None:
    from tests.fakes import FakeFormPage, FakeFormQuestion

    stripped = example_profile.model_copy(update={"answers_bank": []})
    llm = TrackingLlm()
    filler = LeverFormFiller(stripped, llm, FormsConfig())
    question = FakeFormQuestion(
        label="Will you require sponsorship now or in the future?",
        container_selector=".application-question",
        control_name="cards[sponsorship]",
        kind="select",
        required=False,
        options=["Yes", "No"],
    )
    page = FakeFormPage(present=_LEVER_PRESENT, questions=[question])

    result = filler.fill_page(page, _ctx(stripped))

    assert llm.prompts == []
    assert result.unanswered_sensitive == ["Will you require sponsorship now or in the future?"]
    assert not result.aborted
