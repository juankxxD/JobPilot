"""Tests for the Phase 0 credential checks (no real secrets required)."""

from __future__ import annotations

import pytest

from jobpilot.checks import (
    check_drive,
    check_groq,
    check_mail,
    check_neon,
    check_telegram,
)
from jobpilot.checks.base import CheckResult, missing
from jobpilot.config import get_settings

_SECRET_ENV_VARS = [
    "GROQ_API_KEY",
    "DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OWNER_CHAT_ID",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "DRIVE_FOLDER_ID",
    "GRAPH_CLIENT_ID",
    "GRAPH_REFRESH_TOKEN",
    "GMAIL_CLIENT_ID",
]


@pytest.fixture
def no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Point at a non-existent .env so a developer's real one is not picked up.
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")
    get_settings.cache_clear()


def test_missing_helper() -> None:
    result = missing("Svc", "A", "B")
    assert result.ok is False
    assert "A, B" in result.detail


def test_check_result_render() -> None:
    assert CheckResult("Svc", True, "ok").render().startswith("[OK")
    assert CheckResult("Svc", False, "bad").render().startswith("[FAIL")


@pytest.mark.usefixtures("no_secrets")
def test_all_checks_fail_closed_without_secrets() -> None:
    # Each check must return a FAIL result (never raise) when config is missing.
    for check in (check_groq, check_neon, check_telegram, check_drive, check_mail):
        result = check()
        assert isinstance(result, CheckResult)
        assert result.ok is False
