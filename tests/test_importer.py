"""Tests for the CV importer (LLM mocked)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobpilot.llm.schemas import (
    CvImportResult,
    DraftAchievement,
    DraftBasics,
    DraftExperience,
    DraftProject,
)
from jobpilot.profile.importer import (
    UnsupportedFileType,
    extract_text,
    import_cv,
)
from tests.fakes import FakeLlmClient


def _sample_result() -> CvImportResult:
    return CvImportResult(
        basics=DraftBasics(
            name="Jane Doe", email="jane@example.com", phone=None, location="Bogotá"
        ),
        summary_variants={},
        experience=[
            DraftExperience(
                id="exp_acme_2022",
                company="ACME",
                role="Backend Engineer",
                start="2022-01",
                end=None,
                achievements=[
                    # English present, Spanish missing -> must become pending_review.
                    DraftAchievement(
                        id="ach_acme_latency",
                        tags=["java"],
                        es=None,
                        en="Reduced latency by 40%.",
                        translation_status="pending_review",
                    ),
                ],
            )
        ],
        projects=[
            DraftProject(
                id="prj_x",
                name="Project X",
                tags=["python"],
                es="Una app.",
                en="An app.",
                translation_status="approved",
            )
        ],
        uncertain_fields=["basics.phone"],
    )


def test_extract_text_unsupported(tmp_path: Path) -> None:
    f = tmp_path / "cv.txt"
    f.write_text("hi", encoding="utf-8")
    with pytest.raises(UnsupportedFileType):
        extract_text(f)


def test_extract_text_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        extract_text(tmp_path / "nope.pdf")


def test_import_cv_writes_draft_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Avoid real text extraction: feed fixed resume text.
    monkeypatch.setattr(
        "jobpilot.profile.importer.extract_text", lambda _p: "Jane Doe, Backend Engineer at ACME."
    )
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 fake")

    llm = FakeLlmClient(_sample_result())
    draft_path, report = import_cv(cv, llm, output_dir=tmp_path)

    assert draft_path.exists()
    assert draft_path.name == "profile.draft.yaml"

    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))

    # Incomplete translation (es missing) must be flagged pending_review.
    ach = draft["experience"][0]["achievements"][0]
    assert ach["translation_status"] == "pending_review"

    # Scaffolded owner-only sections present.
    assert "answers_bank" in draft
    assert "preferences" in draft

    # Report surfaces missing/uncertain fields.
    assert "basics.phone" in report.empty_fields
    assert "basics.phone" in report.uncertain_fields
    assert "summary_variants" in report.empty_fields
    assert "achievement:ach_acme_latency" in report.pending_translations
    assert report.counts["experiences"] == 1
    assert report.counts["achievements"] == 1

    # The anti-invention prompt was used.
    assert "Use ONLY facts present" in llm.prompts[0]


def test_import_cv_does_not_invent_missing_basics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jobpilot.profile.importer.extract_text", lambda _p: "some resume text")
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 fake")

    # LLM returns no phone/location: the draft must keep them empty, not fabricate.
    result = _sample_result()
    result.basics.location = None
    llm = FakeLlmClient(result)
    draft_path, report = import_cv(cv, llm, output_dir=tmp_path)

    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert draft["basics"]["phone"] is None
    assert draft["basics"]["location"] is None
    assert "basics.location" in report.empty_fields


def test_render_report_is_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobpilot.profile.importer.extract_text", lambda _p: "resume")
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    _, report = import_cv(cv, FakeLlmClient(_sample_result()), output_dir=tmp_path)
    rendered = report.render()
    assert "CV import report" in rendered
    assert "profile.yaml" in rendered
