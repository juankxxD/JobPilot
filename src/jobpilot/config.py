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

    # Google Drive
    google_service_account_json: str | None = None
    drive_folder_id: str | None = None

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


class AppConfig(BaseModel):
    sources: dict[str, SourceFlag] = Field(default_factory=dict)
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    language: LanguageConfig = Field(default_factory=LanguageConfig)
    proposals: ProposalsConfig = Field(default_factory=ProposalsConfig)
    follow_up: FollowUpConfig = Field(default_factory=FollowUpConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)

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
