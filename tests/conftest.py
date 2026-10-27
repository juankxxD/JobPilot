"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def clean_settings_cache() -> None:
    """Clear the cached Settings so env overrides take effect per test."""
    from jobpilot.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def example_profile_dict() -> dict:
    """The committed example profile, parsed to a dict."""
    import yaml

    text = (PROJECT_ROOT / "profile.example.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)
