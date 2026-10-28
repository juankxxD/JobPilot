"""Pipeline wiring: select -> compose cover -> render -> upload, storage faked.

These tests use the HTML render + fake storage path and do not require WeasyPrint;
the PDF path itself is covered (and skipped locally) in ``test_pdf_ats.py``.
"""

from __future__ import annotations

import pytest

from jobpilot.config import Language
from jobpilot.documents.cover import CoverLetterError
from jobpilot.documents.selector import SelectionError
from jobpilot.llm.schemas import CoverLetterDraft
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


def _cover(
    body: str = "I am excited to apply and bring my backend experience to your team.",
) -> CoverLetterDraft:
    return CoverLetterDraft(body=body)


@skip_without_weasyprint
def test_generate_documents_uploads_both(example_profile: ProfileModel) -> None:
    llm = ScriptedFakeLlmClient([_selection(), _cover()])
    storage = FakeFileStorage()
    result = generate_documents(
        _job(),
        example_profile,
        llm,
        storage,
        language=Language.en,
    )
    assert len(storage.uploads) == 2
    assert result.cv_file.web_view_link.startswith("https://drive.example.com/file")
    assert result.cover_letter_file.web_view_link.startswith("https://drive.example.com/file")
    assert result.cv.filename.endswith("_EN.pdf")
    assert result.cover_letter.filename.endswith("_EN.pdf")


def test_generate_documents_rejects_long_cover(example_profile: ProfileModel) -> None:
    """A too-long composed cover is rejected before any upload (no WeasyPrint needed).

    The LLM returns an over-length body on both the attempt and its retry, so the
    composer raises ``CoverLetterError`` and nothing is rendered or uploaded.
    """
    long_body = " ".join(["word"] * 300)
    llm = ScriptedFakeLlmClient([_selection(), _cover(long_body), _cover(long_body)])
    storage = FakeFileStorage()
    with pytest.raises(CoverLetterError):
        generate_documents(
            _job(),
            example_profile,
            llm,
            storage,
            language=Language.en,
        )
    assert storage.uploads == []


def test_generate_documents_rejects_invalid_selection(example_profile: ProfileModel) -> None:
    """An invalid selection fails before cover/render/upload (no WeasyPrint needed)."""
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
        )
    assert storage.uploads == []
