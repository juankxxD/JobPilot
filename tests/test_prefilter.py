"""Edge-case tests for the deterministic prefilter."""

from __future__ import annotations

from jobpilot.config import Language, PrefilterConfig
from jobpilot.filters.prefilter import prefilter
from jobpilot.models import RawJob


def _raw(**overrides: object) -> RawJob:
    base: dict[str, object] = {
        "source": "src",
        "external_id": "e1",
        "url": "https://x/1",
        "title": "Backend Engineer",
        "company": "Acme",
        "description": "java spring backend role",
        "remote": True,
        "location": None,
        "salary_text": None,
    }
    base.update(overrides)
    return RawJob(**base)


def _config(**overrides: object) -> PrefilterConfig:
    base: dict[str, object] = {
        "countries": ["Colombia"],
        "remote_ok": True,
        "required_keywords": ["java", "python"],
        "excluded_keywords": ["unpaid"],
        "accepted_languages": [Language.es, Language.en],
        "modalities": ["remote", "hybrid", "onsite"],
        "min_salary_usd": 0,
    }
    base.update(overrides)
    return PrefilterConfig(**base)


def test_remote_passes_without_matching_country() -> None:
    assert prefilter(_raw(remote=True, location=None), _config()) is True


def test_non_remote_needs_country_match() -> None:
    assert prefilter(_raw(remote=False, location="Bogotá, Colombia"), _config()) is True
    assert prefilter(_raw(remote=False, location="Berlin, Germany"), _config()) is False


def test_excluded_keyword_drops() -> None:
    raw = _raw(description="java backend but unpaid internship")
    assert prefilter(raw, _config()) is False


def test_missing_required_keyword_drops() -> None:
    raw = _raw(description="ruby on rails role")
    assert prefilter(raw, _config()) is False


def test_no_required_keywords_accepts_any() -> None:
    raw = _raw(description="ruby on rails role")
    assert prefilter(raw, _config(required_keywords=[])) is True


def test_salary_below_min_drops_only_when_declared() -> None:
    config = _config(min_salary_usd=5000)
    # Declared salary below the minimum -> drop.
    assert prefilter(_raw(salary_text="3000 USD"), config) is False
    # Declared salary above -> keep.
    assert prefilter(_raw(salary_text="8000 USD"), config) is True
    # No salary declared -> keep (we only drop when the offer declares one).
    assert prefilter(_raw(salary_text=None), config) is True


def test_language_not_accepted_drops() -> None:
    config = _config(accepted_languages=[Language.es])
    assert prefilter(_raw(), config, language=Language.en) is False
    assert prefilter(_raw(), config, language=Language.es) is True
    # No language supplied -> language rule does not apply.
    assert prefilter(_raw(), config, language=None) is True
