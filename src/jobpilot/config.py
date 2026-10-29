"""Application configuration.

Two layers:

* :class:`Settings` — secrets and environment-specific values loaded from the
  environment (and a ``.env`` file in local development).
* :class:`AppConfig` — non-secret, tunable behaviour loaded from ``config.yaml``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class MailProvider(StrEnum):
    outlook = "outlook"
    gmail = "gmail"


class Language(StrEnum):
    es = "es"
    en = "en"


# --------------------------------------------------------------------------- #
# Settings (secrets / environment)
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    """Secrets and environment-specific values.

    Fields are optional at load time so that commands which do not need a given
    credential (e.g. ``migrate``) can run without it. The per-service ``check_*``
    scripts validate that what they need is present.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # LLM: Groq
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"

    # LLM: Ollama (local fallback)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"

    # Database
    database_url: str | None = None

    # Telegram
    telegram_bot_token: str | None = None
    telegram_owner_chat_id: str | None = None

    # Google Drive. Two auth modes: a service account (no storage quota of its own,
    # so it only works with a Shared Drive) or user OAuth (files are owned by the
    # authorizing account). OAuth is used whenever DRIVE_REFRESH_TOKEN is set; the
    # OAuth client defaults to the Google client configured for Gmail.
    google_service_account_json: str | None = None
    drive_folder_id: str | None = None
    drive_refresh_token: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None

    # Mail provider selection
    mail_provider: MailProvider = MailProvider.outlook

    # Outlook / Microsoft Graph
    graph_client_id: str | None = None
    graph_tenant_id: str = "consumers"
    graph_refresh_token: str | None = None
    mail_from_address: str | None = None

    # Gmail
    gmail_client_id: str | None = None
    gmail_client_secret: str | None = None
    gmail_refresh_token: str | None = None

    # Adzuna (optional)
    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None

    # Pipeline behaviour
    min_score: int = 70
    daily_application_limit: int = 5
    default_language: Language = Language.es

    @property
    def drive_oauth_client_id(self) -> str | None:
        """OAuth client id for Drive: the dedicated value, else the Gmail client."""
        return self.google_oauth_client_id or self.gmail_client_id

    @property
    def drive_oauth_client_secret(self) -> str | None:
        """OAuth client secret for Drive: the dedicated value, else the Gmail client."""
        return self.google_oauth_client_secret or self.gmail_client_secret


# --------------------------------------------------------------------------- #
# AppConfig (config.yaml)
# --------------------------------------------------------------------------- #
class SourceFlag(BaseModel):
    enabled: bool = False


class PrefilterConfig(BaseModel):
    countries: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    required_keywords: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    accepted_languages: list[Language] = Field(default_factory=lambda: [Language.es, Language.en])
    modalities: list[str] = Field(default_factory=list)
    min_salary_usd: int = 0


class ScoringWeights(BaseModel):
    required_stack: int = 40
    seniority_and_years: int = 20
    modality_and_timezone: int = 15
    language: int = 10
    salary_and_conditions: int = 10
    other: int = 5

    def total(self) -> int:
        return (
            self.required_stack
            + self.seniority_and_years
            + self.modality_and_timezone
            + self.language
            + self.salary_and_conditions
            + self.other
        )


class ScoringConfig(BaseModel):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)


class LanguageConfig(BaseModel):
    min_confidence: float = 0.80
    use_default_on_timeout: bool = False
    fallback_after_hours: int = 24


class ProposalsConfig(BaseModel):
    dedupe_company_role_days: int = 30


class FollowUpConfig(BaseModel):
    reminder_after_days: int = 7
    weekly_report_weekday: int = 1


class ScheduleConfig(BaseModel):
    cron_hours: int = 2


class FormsConfig(BaseModel):
    """Behaviour of the assisted form filler (Phase 8, LOCAL only).

    These forms are driven by Playwright on the owner's machine and are never run
    in CI. ``headless`` defaults to ``False`` so the owner watches the browser fill
    the form; set it ``True`` only for local debugging. ``slow_mo_ms`` adds a small
    per-action delay so the visible run is easy to follow.
    """

    headless: bool = False
    slow_mo_ms: int = 0


class AppConfig(BaseModel):
    sources: dict[str, SourceFlag] = Field(default_factory=dict)
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    language: LanguageConfig = Field(default_factory=LanguageConfig)
    proposals: ProposalsConfig = Field(default_factory=ProposalsConfig)
    follow_up: FollowUpConfig = Field(default_factory=FollowUpConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    forms: FormsConfig = Field(default_factory=FormsConfig)

    def source_enabled(self, name: str) -> bool:
        flag = self.sources.get(name)
        return bool(flag and flag.enabled)


def load_app_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load and validate ``config.yaml``.

    Raises a clear error if the scoring weights do not sum to 100.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = AppConfig.model_validate(raw)
    total = config.scoring.weights.total()
    if total != 100:
        raise ValueError(f"Scoring weights must sum to 100, got {total}.")
    return config


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached :class:`Settings` loaded from the environment / .env."""
    return Settings()
