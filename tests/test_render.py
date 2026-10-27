"""Render helpers: 200-word cover limit, safe filename slug, lazy PDF smoke test."""

from __future__ import annotations

import pytest

from jobpilot.config import Language
from jobpilot.documents.render import (
    CoverLetterTooLongError,
    build_document_filename,
    render_cover_html,
    render_cv,
    safe_slug,
)
from jobpilot.models import ContentSelection
from jobpilot.profile.model import ProfileModel
from tests.conftest import skip_without_weasyprint


def test_safe_slug_strips_accents_and_spaces() -> None:
    assert safe_slug("José Peña") == "Jose_Pena"
    assert safe_slug("  ACME  Corp. ") == "ACME_Corp"
    assert safe_slug("Señor/Developer") == "Senor_Developer"
    assert safe_slug("R&D Team") == "R_and_D_Team"


def test_build_document_filename_es_and_en() -> None:
    es = build_document_filename(
        full_name="José Peña",
        company="ACME Corp",
        role="Backend Engineer",
        language=Language.es,
    )
    assert es == "Jose_Pena_ACME_Corp_Backend_Engineer_ES.pdf"

    en = build_document_filename(
        full_name="Jane Doe",
        company="Globex",
        role="Software Developer",
        language=Language.en,
        kind="cover",
    )
    assert en == "Jane_Doe_Globex_Software_Developer_cover_EN.pdf"


def test_cover_letter_at_limit_renders(example_profile: ProfileModel) -> None:
    body = " ".join(["word"] * 200)
    html = render_cover_html(example_profile, language=Language.en, body=body, company="ACME")
    assert "word" in html


def test_cover_letter_over_limit_raises(example_profile: ProfileModel) -> None:
    body = " ".join(["word"] * 201)
    with pytest.raises(CoverLetterTooLongError) as excinfo:
        render_cover_html(example_profile, language=Language.en, body=body, company="ACME")
    assert "201" in str(excinfo.value)


def _selection() -> ContentSelection:
    return ContentSelection(
        summary_variant="backend_java",
        ordered_achievement_ids=["ach_acme_latency"],
        project_ids=[],
        highlighted_skill_ids=[],
        language=Language.en,
    )


@skip_without_weasyprint
def test_render_cv_produces_pdf(example_profile: ProfileModel) -> None:
    """Smoke test the full HTML->PDF path; skips locally without pango/cairo."""
    attachment = render_cv(example_profile, _selection(), company="ACME Corp", role="Backend")
    assert attachment.filename.endswith("_EN.pdf")
    assert attachment.content.startswith(b"%PDF")
