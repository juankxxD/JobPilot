"""Validate that profile.example.yaml parses as valid YAML with the expected shape.

The full Pydantic ProfileModel lands in Phase 1; here we assert the example file
is present and structurally complete so later phases have a reference fixture.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = PROJECT_ROOT / "profile.example.yaml"

REQUIRED_TOP_LEVEL_KEYS = {
    "basics",
    "summary_variants",
    "skills",
    "experience",
    "education",
    "certifications",
    "projects",
    "languages_spoken",
    "answers_bank",
    "preferences",
}


def test_example_profile_exists() -> None:
    assert EXAMPLE.exists(), "profile.example.yaml must be committed"


def test_example_profile_has_all_sections() -> None:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert REQUIRED_TOP_LEVEL_KEYS.issubset(data.keys())


def test_example_profile_ids_are_unique() -> None:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    ids: list[str] = []
    for exp in data["experience"]:
        ids.append(exp["id"])
        ids.extend(ach["id"] for ach in exp.get("achievements", []))
    ids.extend(prj["id"] for prj in data["projects"])
    assert len(ids) == len(set(ids)), "all achievement/experience/project ids must be unique"


def test_sensitive_answers_are_flagged() -> None:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    by_key = {a["key"]: a for a in data["answers_bank"]}
    for key in ("salary_expectation", "work_authorization", "relocation"):
        assert by_key[key]["sensitive"] is True
