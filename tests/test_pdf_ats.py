"""ATS read-order test: the generated PDF must expose the localized section
headers in order, for both Spanish and English.

Requires WeasyPrint's native libraries (pango/cairo), so it skips cleanly locally
(where they are absent) and runs in CI (where they are installed via apt).
"""

from __future__ import annotations

import copy
import io
from typing import Any

import pytest

from jobpilot.config import Language
from jobpilot.documents.render import (
    MAX_CV_PAGES,
    CvTooLongError,
    html_to_pdf,
    render_cv,
    render_cv_html,
)
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


def _oversized_profile(example_profile_dict: dict[str, Any]) -> tuple[ProfileModel, list[str]]:
    """Return a valid profile whose single role has enough long achievements to
    overflow two pages, plus the list of those achievement ids (for the selection)."""
    data = copy.deepcopy(example_profile_dict)
    long_text = " ".join(["Delivered a resilient distributed service with measurable impact."] * 20)
    achievements = []
    ids: list[str] = []
    for i in range(40):
        ach_id = f"ach_bulk_{i}"
        ids.append(ach_id)
        achievements.append(
            {
                "id": ach_id,
                "tags": ["Java"],
                "es": long_text,
                "en": long_text,
                "translation_status": "approved",
            }
        )
    data["experience"][0]["achievements"] = achievements
    return ProfileModel.model_validate(data), ids


def test_oversized_valid_cv_is_rejected_not_uploaded(example_profile_dict: dict[str, Any]) -> None:
    """A valid but very long profile must not yield a CV over the two-page maximum.

    ``render_cv`` raises before returning any bytes, so no oversized CV can reach an
    upload. This guards the two-page requirement against unbounded profile content.
    """
    profile, ids = _oversized_profile(example_profile_dict)
    selection = ContentSelection(
        summary_variant="backend_java",
        ordered_achievement_ids=ids,
        project_ids=[],
        highlighted_skill_ids=[],
        language=Language.en,
    )
    with pytest.raises(CvTooLongError) as excinfo:
        render_cv(profile, selection, company="ACME", role="Backend")
    assert str(MAX_CV_PAGES) in str(excinfo.value)
