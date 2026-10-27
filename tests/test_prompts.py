"""Tests for the Phase 3 prompt builders.

Every prompt must embed the anti-invention block and the relevant offer/profile
data; the score prompt must instruct the model to cite an achievement id.
"""

from __future__ import annotations

from jobpilot.config import ScoringWeights
from jobpilot.llm.prompts import (
    ANTI_INVENTION_RULES,
    build_analyze_job_prompt,
    build_confirm_language_prompt,
    build_score_job_prompt,
)

_ANCHOR = ANTI_INVENTION_RULES.splitlines()[0]


def test_analyze_prompt_has_anti_invention_and_offer() -> None:
    prompt = build_analyze_job_prompt(
        title="Backend Engineer",
        company="Globex",
        location="Remote",
        remote=True,
        salary="$100k",
        description="Build services in Java and Spring Boot.",
        schema_json="{}",
    )
    assert _ANCHOR in prompt
    assert "Backend Engineer" in prompt
    assert "Globex" in prompt
    assert "Java and Spring Boot" in prompt


def test_score_prompt_has_anti_invention_weights_and_ids() -> None:
    weights = ScoringWeights()
    prompt = build_score_job_prompt(
        profile_json='{"basics": {"name": "Jane"}}',
        achievement_ids=["ach_acme_latency", "ach_acme_events"],
        analysis_json='{"normalized_title": "Backend"}',
        weights=weights,
        schema_json="{}",
    )
    assert _ANCHOR in prompt
    assert "achievement_id" in prompt
    assert "ach_acme_latency" in prompt
    assert "ach_acme_events" in prompt
    # Weights are communicated to the model.
    assert str(weights.required_stack) in prompt
    assert '"name": "Jane"' in prompt


def test_score_prompt_handles_no_achievement_ids() -> None:
    prompt = build_score_job_prompt(
        profile_json="{}",
        achievement_ids=[],
        analysis_json="{}",
        weights=ScoringWeights(),
        schema_json="{}",
    )
    assert "(none)" in prompt


def test_confirm_language_prompt_has_anti_invention_and_text() -> None:
    prompt = build_confirm_language_prompt(text="Buscamos ingeniero backend", schema_json="{}")
    assert _ANCHOR in prompt
    assert "Buscamos ingeniero backend" in prompt
    # The model may decline es/en with "other" so unsupported offers are not forced.
    assert '"other"' in prompt
