"""Tests for the ProfileModel and its invariants."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from jobpilot.config import Language
from jobpilot.profile.model import ProfileModel, TranslationStatus, load_profile


def test_example_profile_validates(example_profile_dict: dict) -> None:
    profile = ProfileModel.model_validate(example_profile_dict)
    assert profile.basics.name == "Jane Doe"
    assert {"backend_java", "fullstack", "cloud"} <= profile.summary_variants.keys()
    assert "ach_acme_latency" in profile.achievement_ids()
    assert "prj_openbudget" in profile.project_ids()


def test_missing_summary_variant_fails(example_profile_dict: dict) -> None:
    data = copy.deepcopy(example_profile_dict)
    del data["summary_variants"]["cloud"]
    with pytest.raises(ValidationError, match="missing required keys"):
        ProfileModel.model_validate(data)


def test_duplicate_ids_fail(example_profile_dict: dict) -> None:
    data = copy.deepcopy(example_profile_dict)
    data["projects"].append(data["projects"][0])  # duplicate project id
    with pytest.raises(ValidationError, match="duplicate ids"):
        ProfileModel.model_validate(data)


def test_duplicate_answer_keys_fail(example_profile_dict: dict) -> None:
    data = copy.deepcopy(example_profile_dict)
    data["answers_bank"].append(data["answers_bank"][0])
    with pytest.raises(ValidationError, match="duplicate answers_bank keys"):
        ProfileModel.model_validate(data)


def test_unknown_field_rejected(example_profile_dict: dict) -> None:
    data = copy.deepcopy(example_profile_dict)
    data["unexpected"] = 1
    with pytest.raises(ValidationError):
        ProfileModel.model_validate(data)


def test_approved_text_respects_translation_status(example_profile_dict: dict) -> None:
    data = copy.deepcopy(example_profile_dict)
    data["projects"][0]["translation_status"] = "pending_review"
    profile = ProfileModel.model_validate(data)
    project = profile.project_by_id("prj_openbudget")
    assert project is not None
    assert project.translation_status is TranslationStatus.pending_review
    assert project.approved_text(Language.en) is None  # unapproved -> unusable


def test_approved_text_returns_language(example_profile_dict: dict) -> None:
    profile = ProfileModel.model_validate(example_profile_dict)
    ach = profile.achievement_by_id("ach_acme_latency")
    assert ach is not None
    assert ach.approved_text(Language.es) is not None
    assert ach.approved_text(Language.en) is not None


def test_load_profile_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path / "profile.yaml")


def test_load_profile_from_example(tmp_path, example_profile_dict: dict) -> None:
    import yaml

    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(example_profile_dict, allow_unicode=True), encoding="utf-8")
    profile = load_profile(path)
    assert profile.basics.name == "Jane Doe"
