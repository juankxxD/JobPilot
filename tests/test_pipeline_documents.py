"""Pipeline wiring: select -> render -> upload, with the FileStorage faked.

These tests use the HTML render + fake storage path and do not require WeasyPrint;
the PDF path itself is covered (and skipped locally) in ``test_pdf_ats.py``.
"""

from __future__ import annotations

import pytest

from jobpilot.config import Language
from jobpilot.documents.render import CoverLetterTooLongError
from jobpilot.documents.selector import SelectionError
from jobpilot.models import ContentSelection, Job, JobAnalysis
from jobpilot.pipeline.run import generate_documents
from jobpilot.profile.model import ProfileModel
from tests.conftest import skip_without_weasyprint
from tests.fakes import FakeFileStorage, ScriptedFakeLlmClient


def _job() -> Job:
    return Job(
        id=7,
        source="remoteok",
        external_id="rok-7",
        url="https://example.com/job/7",
        title="Backend Engineer",
        company="ACME Corp",
        description="Java, Spring Boot.",
        analysis=JobAnalysis(normalized_title="Backend Engineer", language=Language.en),
    )


def _selection() -> ContentSelection:
    return ContentSelection(
        summary_variant="backend_java",
        ordered_achievement_ids=["ach_acme_latency"],
        project_ids=[],
        highlighted_skill_ids=[],
        language=Language.en,
    )


@skip_without_weasyprint
def test_generate_documents_uploads_both(example_profile: ProfileModel) -> None:
    llm = ScriptedFakeLlmClient([_selection()])
    storage = FakeFileStorage()
    result = generate_documents(
        _job(),
        example_profile,
        llm,
        storage,
        language=Language.en,
        cover_letter_body="Dear team,\n\nI am excited to apply.",
    )
    assert len(storage.uploads) == 2
    assert result.cv_file.web_view_link.startswith("https://drive.example.com/file")
    assert result.cover_letter_file.web_view_link.startswith("https://drive.example.com/file")
    assert result.cv.filename.endswith("_EN.pdf")
    assert result.cover_letter.filename.endswith("_EN.pdf")


def test_generate_documents_rejects_long_cover(example_profile: ProfileModel) -> None:
    """The 200-word guard fires before any upload (no WeasyPrint needed)."""
    llm = ScriptedFakeLlmClient([_selection()])
    storage = FakeFileStorage()
    with pytest.raises(CoverLetterTooLongError):
        generate_documents(
            _job(),
            example_profile,
            llm,
            storage,
            language=Language.en,
            cover_letter_body=" ".join(["word"] * 201),
        )
    assert storage.uploads == []


def test_generate_documents_rejects_invalid_selection(example_profile: ProfileModel) -> None:
    """An invalid selection fails before rendering/uploading (no WeasyPrint needed)."""
    bad = _selection().model_copy(update={"ordered_achievement_ids": ["ach_nope"]})
    llm = ScriptedFakeLlmClient([bad.model_copy(), bad.model_copy()])
    storage = FakeFileStorage()
    with pytest.raises(SelectionError):
        generate_documents(
            _job(),
            example_profile,
            llm,
            storage,
            language=Language.en,
            cover_letter_body="Dear team, I am excited to apply.",
        )
    assert storage.uploads == []
