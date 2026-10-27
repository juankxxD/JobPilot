"""Tests for analysis + weighted scoring and the MIN_SCORE threshold."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from jobpilot.config import AppConfig, ScoringConfig, ScoringWeights
from jobpilot.models import (
    Job,
    JobAnalysis,
    JobScore,
    MetRequirement,
    Recommendation,
    ScoreDimensions,
)
from jobpilot.profile.model import ProfileModel
from jobpilot.scoring.scorer import (
    ScoringError,
    analyze_and_score,
    compute_weighted_score,
    passes_threshold,
)
from tests.fakes import ScriptedFakeLlmClient

Fixture = Callable[[str], Any]


def _job() -> Job:
    return Job(
        id=1,
        source="remoteok",
        external_id="rok-1",
        url="https://example.com/job/1",
        title="Senior Backend Engineer",
        company="Globex",
        description="Java, Spring Boot, PostgreSQL, AWS.",
    )


def test_compute_weighted_score_full_match() -> None:
    weights = ScoringWeights()
    perfect = ScoreDimensions(
        required_stack=100,
        seniority_and_years=100,
        modality_and_timezone=100,
        language=100,
        salary_and_conditions=100,
        other=100,
    )
    assert compute_weighted_score(perfect, weights) == 100


def test_compute_weighted_score_partial() -> None:
    weights = ScoringWeights()  # 40/20/15/10/10/5
    dims = ScoreDimensions(
        required_stack=100,
        seniority_and_years=50,
        modality_and_timezone=0,
        language=100,
        salary_and_conditions=0,
        other=0,
    )
    # 40*1 + 20*0.5 + 15*0 + 10*1 + 10*0 + 5*0 = 60
    assert compute_weighted_score(dims, weights) == 60


def test_custom_weights_change_result() -> None:
    weights = ScoringWeights(
        required_stack=10,
        seniority_and_years=10,
        modality_and_timezone=10,
        language=10,
        salary_and_conditions=10,
        other=50,
    )
    dims = ScoreDimensions(other=100)
    assert compute_weighted_score(dims, weights) == 50


def test_passes_threshold() -> None:
    assert passes_threshold(70, 70) is True
    assert passes_threshold(69, 70) is False
    assert passes_threshold(100, 70) is True


def test_analyze_and_score_valid_ids(example_profile: ProfileModel, load_fixture: Fixture) -> None:
    analysis = JobAnalysis.model_validate(load_fixture("llm_analysis_en.json"))
    score = JobScore.model_validate(load_fixture("llm_score_en.json"))
    llm = ScriptedFakeLlmClient([analysis, score])
    config = AppConfig()

    got_analysis, got_score = analyze_and_score(_job(), example_profile, llm, config)
    assert got_analysis.normalized_title == "Senior Backend Engineer"
    # Aggregate computed in code from the fixture dimensions and default weights
    # (95*40 + 90*20 + 85*15 + 100*10 + 60*10 + 70*5) / 100 = 88.25 -> 88.
    assert got_score.score == 88
    # Two calls: analyse + score.
    assert len(llm.prompts) == 2


def test_analyze_and_score_uses_configured_weights(example_profile: ProfileModel) -> None:
    """The persisted score comes from config weights, never the LLM aggregate."""
    analysis = JobAnalysis(normalized_title="Backend", language=None)
    # The LLM reports a misleading aggregate of 100; only "other" scores highly.
    score = JobScore(
        score=100,
        dimensions=ScoreDimensions(other=80),
        met_requirements=[MetRequirement(requirement="Java", achievement_id="ach_acme_latency")],
        recommendation=Recommendation.apply,
    )
    llm = ScriptedFakeLlmClient([analysis, score])
    # Put all the weight on "other" so the computed aggregate equals 80, not 100.
    config = AppConfig(
        scoring=ScoringConfig(
            weights=ScoringWeights(
                required_stack=0,
                seniority_and_years=0,
                modality_and_timezone=0,
                language=0,
                salary_and_conditions=0,
                other=100,
            )
        )
    )
    _, got_score = analyze_and_score(_job(), example_profile, llm, config)
    assert got_score.score == 80  # fails if the LLM aggregate (100) were trusted


def test_analyze_and_score_retries_on_invalid_id(example_profile: ProfileModel) -> None:
    analysis = JobAnalysis(normalized_title="Backend", language=None)
    bad = JobScore(
        score=80,
        met_requirements=[MetRequirement(requirement="Java", achievement_id="ach_made_up")],
        recommendation=Recommendation.apply,
    )
    good = JobScore(
        score=80,
        met_requirements=[MetRequirement(requirement="Java", achievement_id="ach_acme_latency")],
        recommendation=Recommendation.apply,
    )
    # First attempt: analyse + bad score. Retry: analyse + good score.
    llm = ScriptedFakeLlmClient(
        [
            analysis.model_copy(),
            bad,
            analysis.model_copy(),
            good,
        ]
    )
    _, score = analyze_and_score(_job(), example_profile, llm, AppConfig())
    assert score.met_requirements[0].achievement_id == "ach_acme_latency"
    assert len(llm.prompts) == 4


def test_analyze_and_score_raises_after_retry(example_profile: ProfileModel) -> None:
    analysis = JobAnalysis(normalized_title="Backend", language=None)
    bad = JobScore(
        score=80,
        met_requirements=[MetRequirement(requirement="Java", achievement_id="ach_nope")],
        recommendation=Recommendation.apply,
    )
    llm = ScriptedFakeLlmClient(
        [analysis.model_copy(), bad.model_copy(), analysis.model_copy(), bad.model_copy()]
    )
    with pytest.raises(ScoringError):
        analyze_and_score(_job(), example_profile, llm, AppConfig())
