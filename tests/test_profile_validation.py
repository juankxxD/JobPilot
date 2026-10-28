"""Tests for code-level validation of LLM output against the profile."""

from __future__ import annotations

import copy
from typing import Any

from jobpilot.config import Language
from jobpilot.profile.model import ProfileModel
from jobpilot.profile.validation import (
    extract_mentioned_technologies,
    find_invented_technologies,
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


def test_unknown_highlighted_skill_is_reported(example_profile_dict: dict[str, Any]) -> None:
    profile = _profile(example_profile_dict)
    issues = validate_selection(
        profile,
        summary_variant="backend_java",
        achievement_ids=[],
        project_ids=[],
        language=Language.en,
        highlighted_skill_ids=["Java", "invented-skill"],
    )
    assert issues.unknown_skill_ids == ["invented-skill"]  # Java is in the profile
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


def test_find_invented_technologies_flags_absent_catalogue_tech(
    example_profile_dict: dict[str, Any],
) -> None:
    profile = _profile(example_profile_dict)
    # The example profile has Java/Spring/AWS/Kubernetes but not Rust or Cassandra.
    text = "Built services in Java and migrated the platform to Rust with Cassandra."
    invented = find_invented_technologies(profile, text)
    assert "rust" in invented
    assert "cassandra" in invented
    # Technologies that ARE in the profile must never be flagged as invented.
    assert "java" not in invented
    assert "kubernetes" not in invented


def test_find_invented_technologies_empty_for_profile_only_text(
    example_profile_dict: dict[str, Any],
) -> None:
    profile = _profile(example_profile_dict)
    text = "Experienced in Java, Spring Boot and AWS building backend services."
    assert find_invented_technologies(profile, text) == []


def test_find_invented_technologies_ignores_substrings(
    example_profile_dict: dict[str, Any],
) -> None:
    profile = _profile(example_profile_dict)
    # "go" (a catalogue language absent from the profile) must not match inside
    # ordinary words like "going" or "good".
    text = "I am going to deliver good, reliable software."
    assert "go" not in find_invented_technologies(profile, text)
