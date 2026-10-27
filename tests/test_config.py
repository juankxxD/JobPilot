"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobpilot.config import (
    DEFAULT_CONFIG_PATH,
    Language,
    MailProvider,
    Settings,
    load_app_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_load_default_config() -> None:
    config = load_app_config(DEFAULT_CONFIG_PATH)
    assert config.scoring.weights.total() == 100
    assert config.source_enabled("getonboard") is True
    assert config.source_enabled("adzuna") is False


def test_scoring_weights_must_sum_to_100(tmp_path: Path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text(
        "scoring:\n  weights:\n    required_stack: 50\n    seniority_and_years: 20\n"
        "    modality_and_timezone: 15\n    language: 10\n    salary_and_conditions: 10\n"
        "    other: 5\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must sum to 100"):
        load_app_config(bad)


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_app_config(tmp_path / "does-not-exist.yaml")


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ignore any real .env on the developer machine.
    monkeypatch.chdir(PROJECT_ROOT.parent)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.mail_provider is MailProvider.outlook
    assert settings.min_score == 70
    assert settings.daily_application_limit == 5
    assert settings.default_language is Language.es
    assert settings.groq_model == "openai/gpt-oss-120b"


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_SCORE", "85")
    monkeypatch.setenv("MAIL_PROVIDER", "gmail")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.min_score == 85
    assert settings.mail_provider is MailProvider.gmail
