"""Tests for the Phase 3 LLM schemas (analysis, scoring, language confirmation)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from jobpilot.config import Language
from jobpilot.llm.schemas import ConfirmedLanguage, ConfirmLanguage, JobAnalysis, JobScore
from jobpilot.models import Recommendation

Fixture = Callable[[str], Any]


def test_job_analysis_parses_fixture(load_fixture: Fixture) -> None:
    analysis = JobAnalysis.model_validate(load_fixture("llm_analysis_en.json"))
    assert analysis.normalized_title == "Senior Backend Engineer"
    assert analysis.language is Language.en
    assert "Java" in analysis.required_stack
    assert analysis.years_required == 5


def test_job_score_parses_fixture(load_fixture: Fixture) -> None:
    score = JobScore.model_validate(load_fixture("llm_score_es.json"))
    # The LLM returns per-dimension scores; the aggregate is computed in code, so
    # the raw fixture leaves "score" at its default 0.
    assert score.score == 0
    assert score.dimensions.required_stack == 90
    assert score.dimensions.language == 100
    assert score.recommendation is Recommendation.apply
    assert score.met_requirements[0].achievement_id == "ach_acme_latency"


def test_job_score_rejects_out_of_range(load_fixture: Fixture) -> None:
    payload = load_fixture("llm_score_en.json")
    payload["score"] = 150
    with pytest.raises(ValidationError):
        JobScore.model_validate(payload)


def test_job_score_rejects_missing_dimensions() -> None:
    """A response without a dimension breakdown must not validate (no silent zeros)."""
    with pytest.raises(ValidationError):
        JobScore.model_validate({"score": 99, "recommendation": "apply"})


def test_job_score_rejects_partial_dimensions(load_fixture: Fixture) -> None:
    """A partial breakdown (a dimension omitted) must be rejected, not zero-filled."""
    payload = load_fixture("llm_score_en.json")
    del payload["dimensions"]["other"]
    with pytest.raises(ValidationError):
        JobScore.model_validate(payload)


def test_confirm_language_parses() -> None:
    confirmed = ConfirmLanguage.model_validate({"language": "es", "confidence": 0.9})
    assert confirmed.language is ConfirmedLanguage.es
    assert confirmed.confidence == 0.9


def test_confirm_language_accepts_other() -> None:
    """The confirmation LLM may answer 'other' for an unsupported language."""
    confirmed = ConfirmLanguage.model_validate({"language": "other", "confidence": 0.9})
    assert confirmed.language is ConfirmedLanguage.other


def test_confirm_language_rejects_bad_confidence() -> None:
    with pytest.raises(ValidationError):
        ConfirmLanguage.model_validate({"language": "en", "confidence": 1.5})


def test_confirm_language_rejects_unknown_language() -> None:
    with pytest.raises(ValidationError):
        ConfirmLanguage.model_validate({"language": "fr", "confidence": 0.9})
