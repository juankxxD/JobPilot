"""Tests for code-level validation of LLM output against the profile."""

from __future__ import annotations

import copy
from typing import Any

from jobpilot.config import Language
from jobpilot.profile.model import ProfileModel
from jobpilot.profile.validation import (
    extract_mentioned_technologies,
    find_unknown_technologies,
    validate_selection,
)


def _profile(example_profile_dict: dict[str, Any]) -> ProfileModel:
    return ProfileModel.model_validate(example_profile_dict)


def test_valid_selection_has_no_issues(example_profile_dict: dict[str, Any]) -> None:
    profile = _profile(example_profile_dict)
    issues = validate_selection(
        profile,
        summary_variant="backend_java",
        achievement_ids=["ach_acme_latency", "ach_acme_events"],
        project_ids=["prj_openbudget"],
        language=Language.en,
    )
    assert issues.is_empty(), issues.as_message()


def test_unknown_ids_are_reported(example_profile_dict: dict[str, Any]) -> None:
    profile = _profile(example_profile_dict)
    issues = validate_selection(
        profile,
        summary_variant="does_not_exist",
        achievement_ids=["ach_made_up"],
        project_ids=["prj_made_up"],
        language=Language.en,
    )
    assert issues.unknown_achievement_ids == ["ach_made_up"]
    assert issues.unknown_project_ids == ["prj_made_up"]
    assert issues.unknown_summary_variant == "does_not_exist"
    assert not issues.is_empty()


def test_unapproved_translation_is_reported(example_profile_dict: dict[str, Any]) -> None:
    data = copy.deepcopy(example_profile_dict)
    data["projects"][0]["translation_status"] = "pending_review"
    profile = ProfileModel.model_validate(data)
    issues = validate_selection(
        profile,
        summary_variant="cloud",
        achievement_ids=[],
        project_ids=["prj_openbudget"],
        language=Language.en,
    )
    assert "prj_openbudget" in issues.unapproved_language_ids


def test_find_unknown_technologies(example_profile_dict: dict[str, Any]) -> None:
    profile = _profile(example_profile_dict)
    unknown = find_unknown_technologies(profile, ["Java", "Rust", "COBOL"])
    assert unknown == ["Rust", "COBOL"]  # Java is in the profile


def test_extract_mentioned_technologies(example_profile_dict: dict[str, Any]) -> None:
    profile = _profile(example_profile_dict)
    text = "I built services in Java and deployed them on AWS."
    mentioned = extract_mentioned_technologies(profile, text)
    assert "java" in mentioned
    assert "aws" in mentioned
    assert "mongodb" not in mentioned
