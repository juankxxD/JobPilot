"""Core domain models shared across the pipeline.

These Pydantic models describe jobs, analysis, scoring, document selection and
applications. Persistence-facing models mirror the database tables defined in
``migrations/001_init.sql``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl

from jobpilot.config import Language


class JobStatus(StrEnum):
    new = "new"
    filtered_out = "filtered_out"
    scored = "scored"
    proposed = "proposed"
    approved = "approved"
    applied = "applied"
    discarded = "discarded"
    error = "error"


class Recommendation(StrEnum):
    apply = "apply"
    apply_with_reservations = "apply_with_reservations"
    discard = "discard"


class ApplicationChannel(StrEnum):
    email = "email"
    form_assisted = "form_assisted"
    portal = "portal"
    manual = "manual"


class ApplicationOutcome(StrEnum):
    pending = "pending"
    interview = "interview"
    rejected = "rejected"
    offer = "offer"
    no_response = "no_response"


class RawJob(BaseModel):
    """Normalized output of a fetcher before persistence."""

    source: str
    external_id: str
    url: str
    apply_url: str | None = None
    title: str
    company: str
    location: str | None = None
    remote: bool = False
    salary_text: str | None = None
    contact_email: str | None = None
    description: str = ""
    found_at: datetime | None = None


class JobAnalysis(BaseModel):
    """Structured LLM analysis of a job offer."""

    normalized_title: str
    seniority: str | None = None
    required_stack: list[str] = Field(default_factory=list)
    nice_to_have_stack: list[str] = Field(default_factory=list)
    years_required: int | None = None
    language: Language | None = None
    modality: str | None = None
    timezone: str | None = None
    salary: str | None = None
    contact_email: str | None = None
    apply_platform: str | None = None
    red_flags: list[str] = Field(default_factory=list)


class MetRequirement(BaseModel):
    requirement: str
    achievement_id: str


class JobScore(BaseModel):
    """Structured LLM scoring result (0-100)."""

    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    met_requirements: list[MetRequirement] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    recommendation: Recommendation


class ContentSelection(BaseModel):
    """LLM-chosen CV content, restricted to ids present in the profile."""

    summary_variant: str
    ordered_achievement_ids: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)
    highlighted_skill_ids: list[str] = Field(default_factory=list)
    language: Language


class Job(BaseModel):
    """Persisted job entity (mirrors the ``jobs`` table)."""

    id: int | None = None
    source: str
    external_id: str
    url: str
    apply_url: str | None = None
    title: str
    company: str
    location: str | None = None
    remote: bool = False
    salary_text: str | None = None
    contact_email: str | None = None
    language: Language | None = None
    description: str = ""
    analysis: JobAnalysis | None = None
    score: int | None = None
    score_reason: str | None = None
    missing_requirements: list[str] = Field(default_factory=list)
    status: JobStatus = JobStatus.new
    found_at: datetime | None = None


class Application(BaseModel):
    """Persisted application entity (mirrors the ``applications`` table)."""

    id: int | None = None
    job_id: int
    language: Language
    cv_variant: str
    cv_url: HttpUrl | str | None = None
    cover_letter_url: HttpUrl | str | None = None
    channel: ApplicationChannel
    applied_at: datetime | None = None
    follow_up_at: datetime | None = None
    outcome: ApplicationOutcome = ApplicationOutcome.pending
    notes: str | None = None
    created_at: datetime | None = None


class StoredFile(BaseModel):
    """Result of uploading a file to a :class:`~jobpilot.interfaces.FileStorage`."""

    file_id: str
    name: str
    web_view_link: str


class Attachment(BaseModel):
    filename: str
    content: bytes
    mime_type: str = "application/pdf"
