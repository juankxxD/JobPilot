"""Smoke tests for the Typer CLI surface."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jobpilot.cli import app

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("check-all", "migrate", "import-cv", "auth-mail", "run"):
        assert command in result.output


def test_import_cv_without_api_key_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    # No GROQ_API_KEY -> the command must fail with a clear message, not crash.
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from jobpilot.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["import-cv", "cv.pdf"])
    assert result.exit_code == 1
    assert "GROQ_API_KEY" in result.output


def test_run_without_database_url_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from jobpilot.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "DATABASE_URL" in result.output


def test_migrate_without_database_url_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from jobpilot.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 1
    assert "DATABASE_URL" in result.output
