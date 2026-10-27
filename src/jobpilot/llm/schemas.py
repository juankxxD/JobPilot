"""Pydantic schemas for structured LLM outputs.

Every LLM call returns JSON validated against one of these schemas. Phase 1 only
needs the CV-import schema; later phases add analyze/score/select/cover schemas.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

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
