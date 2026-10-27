"""Shared pytest fixtures."""

from __future__ import annotations

import json
import os
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


def _running_in_ci() -> bool:
    """Return whether we are running in CI.

    GitHub Actions (and most CI providers) set ``CI=true``. In CI the WeasyPrint
    native libraries are installed via apt, so the PDF tests must run — and any
    failure, including a missing-library or rendering regression, must fail the
    job rather than silently skip.
    """
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


def _is_missing_native_lib_error(exc: BaseException) -> bool:
    """Return whether ``exc`` is WeasyPrint failing to load pango/cairo natively.

    WeasyPrint imports in pure Python but loads libgobject/pango/cairo via cffi at
    import/render time; when those system libraries are absent the dlopen fails with
    an ``OSError`` whose message names the missing library. We match only that case
    so an actual template/CSS/rendering regression is NOT mistaken for a missing lib.
    """
    message = str(exc).lower()
    native_markers = ("gobject", "pango", "cairo", "libffi", "fontconfig", "harfbuzz")
    return isinstance(exc, OSError) and any(marker in message for marker in native_markers)


def weasyprint_available() -> bool:
    """Return whether WeasyPrint can render (native pango/cairo libraries present).

    Probes a tiny render. A missing-native-library failure returns ``False`` so the
    PDF tests skip cleanly in a local environment without pango/cairo. Any other
    failure is re-raised so a genuine rendering/template defect surfaces instead of
    being hidden behind a skip. In CI the skip is never applied (see the marker
    below), so missing setup or a rendering regression fails the run.
    """
    try:
        from weasyprint import HTML

        HTML(string="<p>probe</p>").write_pdf()
    except Exception as exc:
        if _running_in_ci():
            raise
        if _is_missing_native_lib_error(exc):
            return False
        raise
    return True


skip_without_weasyprint = pytest.mark.skipif(
    not _running_in_ci() and not weasyprint_available(),
    reason="WeasyPrint native libraries (pango/cairo) are not available",
)


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    """Return a loader for JSON files under ``tests/fixtures``."""

    def _load(name: str) -> Any:
        path = Path(__file__).resolve().parent / "fixtures" / name
        return json.loads(path.read_text(encoding="utf-8"))

    return _load
