"""ATS read-order test: the generated PDF must expose the localized section
headers in order, for both Spanish and English.

Requires WeasyPrint's native libraries (pango/cairo), so it skips cleanly locally
(where they are absent) and runs in CI (where they are installed via apt).
"""

from __future__ import annotations

import io

import pytest

from jobpilot.config import Language
from jobpilot.documents.render import html_to_pdf, render_cv, render_cv_html
from jobpilot.models import ContentSelection
from jobpilot.profile.model import ProfileModel
from tests.conftest import skip_without_weasyprint

pytestmark = skip_without_weasyprint

ES_HEADERS = ["Resumen", "Experiencia", "Educación", "Habilidades", "Certificaciones", "Proyectos"]
EN_HEADERS = ["Summary", "Experience", "Education", "Skills", "Certifications", "Projects"]


def _selection(language: Language) -> ContentSelection:
    return ContentSelection(
        summary_variant="backend_java",
        ordered_achievement_ids=["ach_acme_latency", "ach_acme_events"],
        project_ids=["prj_openbudget"],
        highlighted_skill_ids=[],
        language=language,
    )


def _extract_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _assert_headers_in_order(text: str, headers: list[str]) -> None:
    positions = []
    for header in headers:
        index = text.find(header)
        assert index != -1, f"header {header!r} not found in extracted PDF text"
        positions.append(index)
    assert positions == sorted(positions), f"headers out of order: {positions}"


@pytest.mark.parametrize(
    ("language", "headers"),
    [(Language.es, ES_HEADERS), (Language.en, EN_HEADERS)],
)
def test_pdf_headers_read_order(
    example_profile: ProfileModel, language: Language, headers: list[str]
) -> None:
    attachment = render_cv(example_profile, _selection(language), company="ACME", role="Backend")
    text = _extract_text(attachment.content)
    _assert_headers_in_order(text, headers)


@pytest.mark.parametrize("language", [Language.es, Language.en])
def test_pdf_text_is_selectable(example_profile: ProfileModel, language: Language) -> None:
    """The PDF must contain extractable text (not a flattened image)."""
    html = render_cv_html(example_profile, _selection(language))
    text = _extract_text(html_to_pdf(html))
    assert example_profile.basics.name in text
