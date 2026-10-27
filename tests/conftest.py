"""Shared pytest fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jobpilot.profile.model import ProfileModel

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
def example_profile_dict() -> dict[str, Any]:
    """The committed example profile, parsed to a dict."""
    import yaml

    text = (PROJECT_ROOT / "profile.example.yaml").read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(text)
    return data


@pytest.fixture
def example_profile(example_profile_dict: dict[str, Any]) -> ProfileModel:
    """The committed example profile, validated into a ``ProfileModel``."""
    return ProfileModel.model_validate(example_profile_dict)


def weasyprint_available() -> bool:
    """Return whether WeasyPrint can render (native pango/cairo libraries present).

    WeasyPrint imports in Python even without its native libraries, but rendering a
    PDF fails. We probe a tiny render so the PDF tests skip cleanly locally (no
    native libs) while still running in CI, where the libs are installed via apt.
    """
    try:
        from weasyprint import HTML
    except Exception:  # pragma: no cover - weasyprint is a declared dependency
        return False
    try:
        HTML(string="<p>probe</p>").write_pdf()
    except Exception:
        return False
    return True


skip_without_weasyprint = pytest.mark.skipif(
    not weasyprint_available(),
    reason="WeasyPrint native libraries (pango/cairo) are not available",
)


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    """Return a loader for JSON files under ``tests/fixtures``."""

    def _load(name: str) -> Any:
        path = Path(__file__).resolve().parent / "fixtures" / name
        return json.loads(path.read_text(encoding="utf-8"))

    return _load
