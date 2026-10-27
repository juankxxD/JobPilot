"""CV import: PDF/DOCX text extraction + LLM structuring into a draft profile.

Produces ``profile.draft.yaml`` and a report of empty/uncertain fields. The draft
is never promoted automatically; the owner reviews it and renames it to
``profile.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from jobpilot.llm.prompts import build_cv_import_prompt
from jobpilot.llm.schemas import CvImportResult
from jobpilot.logging import get_logger

if TYPE_CHECKING:
    from jobpilot.interfaces import LlmClient

log = get_logger(__name__)

DRAFT_FILENAME = "profile.draft.yaml"
PENDING = "pending_review"


class UnsupportedFileType(ValueError):
    """Raised for an unsupported CV file extension."""


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #
def extract_text(path: Path | str) -> str:
    """Extract raw text from a PDF or DOCX file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CV file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    raise UnsupportedFileType(f"Unsupported CV file type '{suffix}'. Use .pdf or .docx.")


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _extract_docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    return "\n".join(p.text for p in document.paragraphs).strip()


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ImportReport:
    """Summary of what was extracted, missing, or uncertain."""

    empty_fields: list[str] = field(default_factory=list)
    uncertain_fields: list[str] = field(default_factory=list)
    pending_translations: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        lines = ["CV import report", "=" * 40, "Extracted:"]
        for name, count in self.counts.items():
            lines.append(f"  - {name}: {count}")

        def _section(title: str, items: list[str]) -> None:
            lines.append(f"\n{title} ({len(items)}):")
            if items:
                lines.extend(f"  - {item}" for item in items)
            else:
                lines.append("  (none)")

        _section("Empty / missing fields", self.empty_fields)
        _section("Uncertain fields", self.uncertain_fields)
        _section("Translations pending review", self.pending_translations)
        lines.append("=" * 40)
        lines.append("Review and correct the draft, then rename it to profile.yaml.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Draft assembly
# --------------------------------------------------------------------------- #
def _mark_pending_if_incomplete(block: dict[str, Any]) -> None:
    """Set translation_status to pending_review unless both languages are present."""
    es = block.get("es")
    en = block.get("en")
    if not es or not en:
        block["translation_status"] = PENDING


def _build_draft_dict(result: CvImportResult) -> dict[str, Any]:
    """Turn the LLM result into the profile.yaml structure (as plain dict)."""
    data: dict[str, Any] = result.model_dump(exclude={"uncertain_fields"})

    # Every localized block missing a language stays pending_review.
    for exp in data.get("experience", []):
        for ach in exp.get("achievements", []):
            _mark_pending_if_incomplete(ach)
    for prj in data.get("projects", []):
        _mark_pending_if_incomplete(prj)

    # answers_bank and preferences are owner-provided, not extracted: scaffold them.
    data.setdefault("answers_bank", [])
    data.setdefault(
        "preferences",
        {
            "target_roles": [],
            "countries": [],
            "remote": True,
            "min_salary": 0,
            "accepted_languages": ["es", "en"],
        },
    )
    return data


def _collect_report(result: CvImportResult, draft: dict[str, Any]) -> ImportReport:
    report = ImportReport()
    report.counts = {
        "experiences": len(draft.get("experience", [])),
        "achievements": sum(len(e.get("achievements", [])) for e in draft.get("experience", [])),
        "education": len(draft.get("education", [])),
        "certifications": len(draft.get("certifications", [])),
        "projects": len(draft.get("projects", [])),
        "spoken_languages": len(draft.get("languages_spoken", [])),
    }

    basics = draft.get("basics", {})
    for name in ("name", "email", "phone", "location"):
        if not basics.get(name):
            report.empty_fields.append(f"basics.{name}")

    if not draft.get("summary_variants"):
        report.empty_fields.append("summary_variants")
    if not draft.get("experience"):
        report.empty_fields.append("experience")

    report.uncertain_fields = list(result.uncertain_fields)

    for exp in draft.get("experience", []):
        for ach in exp.get("achievements", []):
            if ach.get("translation_status") == PENDING:
                report.pending_translations.append(f"achievement:{ach.get('id')}")
    for prj in draft.get("projects", []):
        if prj.get("translation_status") == PENDING:
            report.pending_translations.append(f"project:{prj.get('id')}")

    return report


def import_cv(
    cv_path: Path | str,
    llm: LlmClient,
    *,
    output_dir: Path | str | None = None,
) -> tuple[Path, ImportReport]:
    """Import a CV into ``profile.draft.yaml`` and return the path and report."""
    resume_text = extract_text(cv_path)
    if not resume_text:
        raise ValueError(f"No text could be extracted from {cv_path}.")

    schema_json = yaml.safe_dump(CvImportResult.model_json_schema(), sort_keys=False)
    prompt = build_cv_import_prompt(resume_text, schema_json)
    result = llm.complete_json(prompt, CvImportResult)

    draft = _build_draft_dict(result)
    report = _collect_report(result, draft)

    out_dir = Path(output_dir) if output_dir else Path.cwd()
    draft_path = out_dir / DRAFT_FILENAME
    draft_path.write_text(
        yaml.safe_dump(draft, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    log.info("cv_imported", draft=str(draft_path), **report.counts)
    return draft_path, report
