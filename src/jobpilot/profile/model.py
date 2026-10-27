"""Pydantic v2 model for ``profile.yaml`` — the single source of truth.

The model validates structure and enforces the invariants the rest of the system
relies on: unique ids, a valid summary-variant set, and per-text translation
status. Only ``profile.yaml`` is loaded in production; ``profile.draft.yaml`` is a
reviewable intermediate produced by ``jobpilot import-cv`` and is never loaded here.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from jobpilot.config import Language

SUMMARY_VARIANT_KEYS = frozenset({"backend_java", "fullstack", "cloud"})


class TranslationStatus(StrEnum):
    approved = "approved"
    pending_review = "pending_review"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Links(_Base):
    github: str | None = None
    linkedin: str | None = None
    website: str | None = None
    other: dict[str, str] = Field(default_factory=dict)


class Basics(_Base):
    name: str
    email: EmailStr
    phone: str | None = None
    location: str | None = None
    links: Links = Field(default_factory=Links)


class LocalizedText(_Base):
    """A text available in both languages with a translation status.

    ``approved_text`` returns the text for a language only if it is marked
    approved, enforcing the rule that unapproved translations are never used.
    """

    es: str
    en: str
    translation_status: TranslationStatus = TranslationStatus.approved

    def approved_text(self, language: Language) -> str | None:
        if self.translation_status is not TranslationStatus.approved:
            return None
        return self.es if language is Language.es else self.en


class SummaryVariant(_Base):
    es: str
    en: str

    def text(self, language: Language) -> str:
        return self.es if language is Language.es else self.en


class Skills(_Base):
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    cloud: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)

    def all(self) -> list[str]:
        return [*self.languages, *self.frameworks, *self.cloud, *self.databases]


class Achievement(_Base):
    id: str
    tags: list[str] = Field(default_factory=list)
    es: str
    en: str
    translation_status: TranslationStatus = TranslationStatus.approved

    def approved_text(self, language: Language) -> str | None:
        if self.translation_status is not TranslationStatus.approved:
            return None
        return self.es if language is Language.es else self.en


class Experience(_Base):
    id: str
    company: str
    role: str
    start: str
    end: str | None = None  # None = current
    achievements: list[Achievement] = Field(default_factory=list)


class Education(_Base):
    id: str
    institution: str
    degree: str
    start: str | None = None
    end: str | None = None


class Certification(_Base):
    id: str
    name: str
    issuer: str | None = None
    date: str | None = None


class Project(_Base):
    id: str
    name: str
    tags: list[str] = Field(default_factory=list)
    es: str
    en: str
    translation_status: TranslationStatus = TranslationStatus.approved

    def approved_text(self, language: Language) -> str | None:
        if self.translation_status is not TranslationStatus.approved:
            return None
        return self.es if language is Language.es else self.en


class SpokenLanguage(_Base):
    language: str
    level: str


class AnswerBankEntry(_Base):
    key: str
    question_patterns: list[str] = Field(default_factory=list)
    sensitive: bool = False
    es: str
    en: str

    def text(self, language: Language) -> str:
        return self.es if language is Language.es else self.en


class Preferences(_Base):
    target_roles: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    remote: bool = True
    min_salary: int = 0
    accepted_languages: list[Language] = Field(default_factory=lambda: [Language.es, Language.en])


class ProfileModel(_Base):
    """The validated profile."""

    basics: Basics
    summary_variants: dict[str, SummaryVariant]
    skills: Skills
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    languages_spoken: list[SpokenLanguage] = Field(default_factory=list)
    answers_bank: list[AnswerBankEntry] = Field(default_factory=list)
    preferences: Preferences

    # ----------------------------- validators ----------------------------- #
    @model_validator(mode="after")
    def _check_summary_variants(self) -> Self:
        missing = SUMMARY_VARIANT_KEYS - self.summary_variants.keys()
        if missing:
            raise ValueError(f"summary_variants is missing required keys: {sorted(missing)}")
        return self

    @model_validator(mode="after")
    def _check_unique_ids(self) -> Self:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for id_ in self._iter_ids():
            if id_ in seen:
                duplicates.add(id_)
            seen.add(id_)
        if duplicates:
            raise ValueError(f"duplicate ids found in profile: {sorted(duplicates)}")
        return self

    @model_validator(mode="after")
    def _check_unique_answer_keys(self) -> Self:
        keys = [entry.key for entry in self.answers_bank]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            raise ValueError(f"duplicate answers_bank keys: {sorted(dupes)}")
        return self

    # ------------------------------- helpers ------------------------------- #
    def _iter_ids(self) -> list[str]:
        ids: list[str] = []
        for exp in self.experience:
            ids.append(exp.id)
            ids.extend(a.id for a in exp.achievements)
        ids.extend(e.id for e in self.education)
        ids.extend(c.id for c in self.certifications)
        ids.extend(p.id for p in self.projects)
        return ids

    def achievement_ids(self) -> set[str]:
        return {a.id for exp in self.experience for a in exp.achievements}

    def project_ids(self) -> set[str]:
        return {p.id for p in self.projects}

    def achievement_by_id(self, id_: str) -> Achievement | None:
        for exp in self.experience:
            for ach in exp.achievements:
                if ach.id == id_:
                    return ach
        return None

    def project_by_id(self, id_: str) -> Project | None:
        return next((p for p in self.projects if p.id == id_), None)

    def answer_by_key(self, key: str) -> AnswerBankEntry | None:
        return next((a for a in self.answers_bank if a.key == key), None)


def load_profile(path: Path | str) -> ProfileModel:
    """Load and validate ``profile.yaml``.

    Raises a clear error if the file is missing or fails validation.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Profile not found: {path}. Copy profile.example.yaml to profile.yaml "
            "or run `jobpilot import-cv`."
        )
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ProfileModel.model_validate(raw)
