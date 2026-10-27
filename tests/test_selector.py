"""Tests for LLM content selection with code-level profile validation."""

from __future__ import annotations

from typing import Any

import pytest

from jobpilot.config import Language
from jobpilot.documents.selector import SelectionError, select_content
from jobpilot.models import ContentSelection, JobAnalysis
from jobpilot.profile.model import ProfileModel
from tests.fakes import ScriptedFakeLlmClient


def _analysis() -> JobAnalysis:
    return JobAnalysis(
        normalized_title="Senior Backend Engineer",
        required_stack=["Java", "Spring Boot"],
        language=Language.en,
    )


def _valid_selection(language: Language = Language.en) -> ContentSelection:
    return ContentSelection(
        summary_variant="backend_java",
        ordered_achievement_ids=["ach_acme_latency", "ach_acme_events"],
        project_ids=["prj_openbudget"],
        highlighted_skill_ids=["Java", "AWS"],
        language=language,
    )


def test_select_content_valid(example_profile: ProfileModel) -> None:
    llm = ScriptedFakeLlmClient([_valid_selection()])
    selection = select_content(_analysis(), example_profile, llm, Language.en)
    assert selection.summary_variant == "backend_java"
    assert selection.ordered_achievement_ids == ["ach_acme_latency", "ach_acme_events"]
    assert selection.language is Language.en
    assert len(llm.prompts) == 1


def test_select_content_nonexistent_id_fails_after_retry(example_profile: ProfileModel) -> None:
    bad = _valid_selection().model_copy(update={"ordered_achievement_ids": ["ach_does_not_exist"]})
    # Both attempts return the same invalid selection -> SelectionError after retry.
    llm = ScriptedFakeLlmClient([bad.model_copy(), bad.model_copy()])
    with pytest.raises(SelectionError) as excinfo:
        select_content(_analysis(), example_profile, llm, Language.en)
    assert "ach_does_not_exist" in str(excinfo.value)
    assert len(llm.prompts) == 2


def test_select_content_recovers_on_retry(example_profile: ProfileModel) -> None:
    bad = _valid_selection().model_copy(update={"summary_variant": "made_up"})
    good = _valid_selection()
    llm = ScriptedFakeLlmClient([bad, good])
    selection = select_content(_analysis(), example_profile, llm, Language.en)
    assert selection.summary_variant == "backend_java"
    assert len(llm.prompts) == 2


def test_select_content_unapproved_translation_rejected(
    example_profile_dict: dict[str, Any],
) -> None:
    """A text whose translation is not approved for the target language is rejected."""
    # Mark one achievement's translation as pending so EN is not approved.
    for exp in example_profile_dict["experience"]:
        for ach in exp.get("achievements", []):
            if ach["id"] == "ach_acme_latency":
                ach["translation_status"] = "pending_review"
    profile = ProfileModel.model_validate(example_profile_dict)

    selection = _valid_selection().model_copy(
        update={"ordered_achievement_ids": ["ach_acme_latency"], "project_ids": []}
    )
    llm = ScriptedFakeLlmClient([selection.model_copy(), selection.model_copy()])
    with pytest.raises(SelectionError) as excinfo:
        select_content(_analysis(), profile, llm, Language.en)
    assert "unapproved" in str(excinfo.value).lower()


def test_select_content_pins_requested_language(example_profile: ProfileModel) -> None:
    """Even if the LLM returns a different language, selection is pinned to the request."""
    selection = _valid_selection(language=Language.es)
    llm = ScriptedFakeLlmClient([selection])
    result = select_content(_analysis(), example_profile, llm, Language.en)
    assert result.language is Language.en
