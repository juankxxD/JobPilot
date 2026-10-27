"""Render ATS-friendly CV and cover-letter PDFs from the profile and a selection.

The templates (``templates/cv_*.html``, ``cover_*.html`` with ``styles.css``) are
single column with no tables, images or icons, use common fonts and keep text
selectable, so an ATS parser reads them top to bottom in a predictable order.

WeasyPrint needs native libraries (pango/cairo) that are present in CI but not
necessarily locally, so it is imported lazily inside :func:`html_to_pdf`: the
module (and the HTML rendering and validation helpers) import and run without
those libraries. Only the actual PDF step requires them.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobpilot.config import Language
from jobpilot.models import Attachment

if TYPE_CHECKING:
    from jobpilot.models import ContentSelection
    from jobpilot.profile.model import ProfileModel

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
MAX_COVER_WORDS = 200


class CoverLetterTooLongError(ValueError):
    """Raised when a cover letter exceeds the 200-word limit."""


class DocumentContentError(ValueError):
    """Raised when the profile cannot supply an approved text the selection needs."""


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


@lru_cache(maxsize=1)
def _styles() -> str:
    return (TEMPLATES_DIR / "styles.css").read_text(encoding="utf-8")


def count_words(text: str) -> int:
    """Count whitespace-delimited words in ``text``."""
    return len(text.split())


def safe_slug(value: str) -> str:
    """Return an ASCII, filename-safe slug.

    Accents are stripped (``José`` -> ``Jose``), spaces and separators collapse to
    a single underscore, and any other character is dropped, so the result is safe
    to embed in a filename on any filesystem.
    """
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_only = ascii_only.replace("&", " and ")
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", ascii_only)
    return cleaned.strip("_")


def build_document_filename(
    *,
    full_name: str,
    company: str,
    role: str,
    language: Language,
    kind: str = "cv",
) -> str:
    """Build ``Nombre_Apellido_Empresa_Cargo_ES.pdf`` / ``_EN.pdf`` safely.

    Every component is slugged, so accents and spaces never reach the filesystem.
    ``kind`` distinguishes the CV from the cover letter while keeping the required
    ``..._ES.pdf`` / ``..._EN.pdf`` suffix.
    """
    parts = [safe_slug(full_name), safe_slug(company), safe_slug(role)]
    if kind != "cv":
        parts.append(safe_slug(kind))
    stem = "_".join(p for p in parts if p)
    suffix = language.value.upper()
    return f"{stem}_{suffix}.pdf"


def _links(profile: ProfileModel) -> list[str]:
    links = profile.basics.links
    values = [links.github, links.linkedin, links.website, *links.other.values()]
    return [v for v in values if v]


def build_cv_context(profile: ProfileModel, selection: ContentSelection) -> dict[str, object]:
    """Build the Jinja2 context for a CV from the profile and validated selection.

    Only approved texts for the selected language are used; a missing approved
    translation raises :class:`DocumentContentError` rather than silently dropping
    content (callers validate the selection first, so this is a safety net).
    """
    language = selection.language
    variant = profile.summary_variants[selection.summary_variant]
    summary = variant.text(language)

    experiences: list[dict[str, object]] = []
    selected_ach = set(selection.ordered_achievement_ids)
    for exp in profile.experience:
        # Preserve the LLM's requested achievement order within each role.
        ordered = [
            a
            for ach_id in selection.ordered_achievement_ids
            for a in exp.achievements
            if a.id == ach_id
        ]
        texts: list[str] = []
        for ach in ordered:
            text = ach.approved_text(language)
            if text is None:
                raise DocumentContentError(f"achievement {ach.id} has no approved {language.value}")
            texts.append(text)
        if not any(a.id in selected_ach for a in exp.achievements):
            continue
        experiences.append(
            {
                "company": exp.company,
                "role": exp.role,
                "start": exp.start,
                "end": exp.end,
                "achievements": texts,
            }
        )

    projects: list[dict[str, object]] = []
    for prj_id in selection.project_ids:
        prj = profile.project_by_id(prj_id)
        if prj is None:
            raise DocumentContentError(f"unknown project id {prj_id}")
        text = prj.approved_text(language)
        if text is None:
            raise DocumentContentError(f"project {prj.id} has no approved {language.value}")
        projects.append({"name": prj.name, "text": text})

    known_skills = {s.lower(): s for s in profile.skills.all()}
    highlighted = [
        known_skills[s.lower()]
        for s in selection.highlighted_skill_ids
        if s.lower() in known_skills
    ]
    skills = highlighted or profile.skills.all()

    return {
        "styles": _styles(),
        "basics": {
            "name": profile.basics.name,
            "email": profile.basics.email,
            "phone": profile.basics.phone,
            "location": profile.basics.location,
            "links": _links(profile),
        },
        "summary": summary,
        "experiences": experiences,
        "education": [
            {
                "institution": edu.institution,
                "degree": edu.degree,
                "start": edu.start,
                "end": edu.end,
            }
            for edu in profile.education
        ],
        "skills": skills,
        "certifications": [
            {"name": c.name, "issuer": c.issuer, "date": c.date} for c in profile.certifications
        ],
        "projects": projects,
    }


def render_cv_html(profile: ProfileModel, selection: ContentSelection) -> str:
    """Render the CV template to an HTML string (no WeasyPrint needed)."""
    template_name = "cv_es.html" if selection.language is Language.es else "cv_en.html"
    context = build_cv_context(profile, selection)
    return _env().get_template(template_name).render(**context)


def render_cover_html(
    profile: ProfileModel,
    *,
    language: Language,
    body: str,
    company: str | None = None,
) -> str:
    """Render the cover-letter template to HTML, enforcing the 200-word limit.

    The limit is validated on the body BEFORE rendering so an over-length letter
    never reaches the PDF step.
    """
    words = count_words(body)
    if words > MAX_COVER_WORDS:
        raise CoverLetterTooLongError(
            f"cover letter has {words} words, the limit is {MAX_COVER_WORDS}"
        )
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    template_name = "cover_es.html" if language is Language.es else "cover_en.html"
    context = {
        "styles": _styles(),
        "basics": {
            "name": profile.basics.name,
            "email": profile.basics.email,
            "phone": profile.basics.phone,
            "location": profile.basics.location,
        },
        "company": company,
        "paragraphs": paragraphs,
    }
    return _env().get_template(template_name).render(**context)


def html_to_pdf(html: str) -> bytes:
    """Convert an HTML string to PDF bytes with WeasyPrint.

    WeasyPrint is imported here (lazily) because it needs native libraries that may
    be missing locally; importing this module never requires them.
    """
    from weasyprint import HTML  # noqa: PLC0415 - lazy import for optional native deps

    return HTML(string=html).write_pdf()  # type: ignore[no-any-return]


def render_cv(
    profile: ProfileModel,
    selection: ContentSelection,
    *,
    company: str,
    role: str,
) -> Attachment:
    """Render a tailored CV to a PDF :class:`~jobpilot.models.Attachment`."""
    html = render_cv_html(profile, selection)
    pdf = html_to_pdf(html)
    filename = build_document_filename(
        full_name=profile.basics.name,
        company=company,
        role=role,
        language=selection.language,
        kind="cv",
    )
    return Attachment(filename=filename, content=pdf)


def render_cover_letter(
    profile: ProfileModel,
    *,
    language: Language,
    body: str,
    company: str,
    role: str,
) -> Attachment:
    """Render a cover letter to a PDF :class:`~jobpilot.models.Attachment`.

    The 200-word limit is enforced by :func:`render_cover_html` before the PDF step.
    """
    html = render_cover_html(profile, language=language, body=body, company=company)
    pdf = html_to_pdf(html)
    filename = build_document_filename(
        full_name=profile.basics.name,
        company=company,
        role=role,
        language=language,
        kind="cover",
    )
    return Attachment(filename=filename, content=pdf)
