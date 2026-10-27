"""Edge-case tests for the deterministic prefilter."""

from __future__ import annotations

from jobpilot.config import Language, PrefilterConfig
from jobpilot.filters.prefilter import prefilter
from jobpilot.models import Modality, RawJob


def _raw(**overrides: object) -> RawJob:
    base: dict[str, object] = {
        "source": "src",
        "external_id": "e1",
        "url": "https://x/1",
        "title": "Backend Engineer",
        "company": "Acme",
        "description": "java spring backend role",
        "remote": True,
        "modality": Modality.remote,
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


def test_salary_k_suffix_is_annualized_to_monthly() -> None:
    config = _config(min_salary_usd=5000)
    # The Remotive fixture shape "$80k - $110k USD" is 80000-110000 yearly, i.e.
    # ~6666 USD/month, which clears a 5000 monthly threshold (must not be read as 80).
    assert prefilter(_raw(salary_text="$80k - $110k USD"), config) is True
    # A genuinely low annual salary ($36k -> 3000/month) must still be dropped.
    assert prefilter(_raw(salary_text="$36k USD per year"), config) is False


def test_modality_hybrid_only_rejects_onsite_and_remote() -> None:
    config = _config(modalities=["hybrid"])
    loc = "Bogotá, Colombia"
    assert prefilter(_raw(modality=Modality.hybrid, remote=False, location=loc), config) is True
    assert prefilter(_raw(modality=Modality.onsite, remote=False, location=loc), config) is False
    # A remote job is rejected when only hybrid is allowed.
    assert prefilter(_raw(modality=Modality.remote), config) is False


def test_modality_onsite_only_rejects_hybrid() -> None:
    config = _config(modalities=["onsite"])
    loc = "Bogotá, Colombia"
    assert prefilter(_raw(modality=Modality.onsite, remote=False, location=loc), config) is True
    assert prefilter(_raw(modality=Modality.hybrid, remote=False, location=loc), config) is False


def test_modality_undeclared_is_not_filtered() -> None:
    config = _config(modalities=["remote"])
    assert prefilter(_raw(modality=None), config) is True


def test_language_not_accepted_drops() -> None:
    config = _config(accepted_languages=[Language.es])
    assert prefilter(_raw(), config, language=Language.en) is False
    assert prefilter(_raw(), config, language=Language.es) is True
    # No language supplied -> language rule does not apply.
    assert prefilter(_raw(), config, language=None) is True


def test_declared_source_language_is_used_when_no_override() -> None:
    config = _config(accepted_languages=[Language.es])
    # The job itself declares a disallowed language -> dropped without an override.
    assert prefilter(_raw(language=Language.en), config) is False
    assert prefilter(_raw(language=Language.es), config) is True
