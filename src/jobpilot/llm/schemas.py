"""Pydantic schemas for structured LLM outputs.

Every LLM call returns JSON validated against one of these schemas. Phase 1 only
needs the CV-import schema; later phases add analyze/score/select/cover schemas.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

# The analysis and scoring LLM calls reuse the domain models in ``models.py``
# (``JobAnalysis`` and ``JobScore``), which are already shaped as the structured
# outputs we want. They are re-exported here so callers can import every LLM
# schema from a single module.
from jobpilot.models import JobAnalysis, JobScore

# --------------------------------------------------------------------------- #
# Analysis, scoring and language confirmation (Phase 3)
# --------------------------------------------------------------------------- #

__all__ = [
    "ConfirmLanguage",
    "ConfirmedLanguage",
    "CoverLetterDraft",
    "CvImportResult",
    "FormAnswer",
    "JobAnalysis",
    "JobScore",
]


class FormAnswer(BaseModel):
    """An LLM answer to a single open (non-sensitive) application-form question.

    The model answers ONLY from the candidate profile and the analysed offer; it
    never invents facts. ``confident`` lets it flag an answer it is unsure about so
    the field is marked uncertain for the owner to review before submit. Sensitive
    questions (salary, work authorization/visa, relocation, background/legal,
    demographics, years-of-experience in a specific technology) are never routed to
    this call; they come only from the approved ``answers_bank``.
    """

    answer: str = Field(min_length=1)
    confident: bool = True


class CoverLetterDraft(BaseModel):
    """A cover-letter body the LLM composes from approved profile facts only.

    The LLM only rewrites/arranges facts already present in the profile summary and
    selected achievements; it never invents a new fact or technology. The resulting
    ``body`` is validated in code (word limit, profile-only technologies, and that
    the owner's free-text instruction is never echoed) before it reaches a document,
    so an ``Ajustar`` instruction such as "make it more formal" can change the
    wording without leaking the instruction text or fabricating content.
    """

    body: str = Field(min_length=1)


class ConfirmedLanguage(StrEnum):
    """What the confirmation LLM may answer: a supported language or ``other``.

    ``other`` lets the model say the offer is written in neither Spanish nor
    English, so an unsupported offer is never forced into es/en. It maps to a
    ``None`` detection result, which routes the offer to the Telegram
    language-choice flow instead of persisting a wrong language.
    """

    es = "es"
    en = "en"
    other = "other"


class ConfirmLanguage(BaseModel):
    """LLM confirmation of an offer's language when detection is low-confidence."""

    language: ConfirmedLanguage
    confidence: float = Field(ge=0.0, le=1.0)


# --------------------------------------------------------------------------- #
# CV import (Phase 1)
# --------------------------------------------------------------------------- #
# The import LLM returns a profile-shaped draft. We keep it permissive (strings
# optional) because the whole point is to surface what is missing or uncertain;
# the strict ProfileModel is applied only after the owner reviews the draft.


class DraftLocalized(BaseModel):
    es: str | None = None
    en: str | None = None
    translation_status: str = "pending_review"


class DraftSummaryVariant(BaseModel):
    es: str | None = None
    en: str | None = None


class DraftLink(BaseModel):
    github: str | None = None
    linkedin: str | None = None
    website: str | None = None


class DraftBasics(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: DraftLink = Field(default_factory=DraftLink)


class DraftSkills(BaseModel):
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    cloud: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)


class DraftAchievement(BaseModel):
    id: str
    tags: list[str] = Field(default_factory=list)
    es: str | None = None
    en: str | None = None
    translation_status: str = "pending_review"


class DraftExperience(BaseModel):
    id: str
    company: str | None = None
    role: str | None = None
    start: str | None = None
    end: str | None = None
    achievements: list[DraftAchievement] = Field(default_factory=list)


class DraftEducation(BaseModel):
    id: str
    institution: str | None = None
    degree: str | None = None
    start: str | None = None
    end: str | None = None


class DraftCertification(BaseModel):
    id: str
    name: str | None = None
    issuer: str | None = None
    date: str | None = None


class DraftProject(BaseModel):
    id: str
    name: str | None = None
    tags: list[str] = Field(default_factory=list)
    es: str | None = None
    en: str | None = None
    translation_status: str = "pending_review"


class DraftSpokenLanguage(BaseModel):
    language: str | None = None
    level: str | None = None


class CvImportResult(BaseModel):
    """What the import LLM returns, before human review."""

    basics: DraftBasics = Field(default_factory=DraftBasics)
    summary_variants: dict[str, DraftSummaryVariant] = Field(default_factory=dict)
    skills: DraftSkills = Field(default_factory=DraftSkills)
    experience: list[DraftExperience] = Field(default_factory=list)
    education: list[DraftEducation] = Field(default_factory=list)
    certifications: list[DraftCertification] = Field(default_factory=list)
    projects: list[DraftProject] = Field(default_factory=list)
    languages_spoken: list[DraftSpokenLanguage] = Field(default_factory=list)
    # Fields the model could not confidently extract, for the report.
    uncertain_fields: list[str] = Field(default_factory=list)
