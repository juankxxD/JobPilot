"""Analysis and configurable weighted scoring of a job offer.

``analyze_and_score`` runs the two LLM calls (analyse, then score) and enforces the
anti-invention guarantee in code: every ``achievement_id`` the scorer cites in
``met_requirements`` must exist in the profile. On an invalid id the whole analyse
+ score step is retried once; if it still cites an unknown id a
:class:`ScoringError` is raised so the caller can mark the job ``error``.

The LLM is instructed to return an overall 0-100 score using the configured
weights. ``compute_weighted_score`` provides the same weighted model in code so the
weighting is deterministic and testable, and ``passes_threshold`` applies
``MIN_SCORE``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jobpilot.config import ScoringWeights
from jobpilot.llm.prompts import build_analyze_job_prompt, build_score_job_prompt
from jobpilot.logging import get_logger
from jobpilot.models import JobAnalysis, JobScore

if TYPE_CHECKING:
    from jobpilot.config import AppConfig
    from jobpilot.interfaces import LlmClient
    from jobpilot.models import Job
    from jobpilot.profile.model import ProfileModel

log = get_logger(__name__)


class ScoringError(RuntimeError):
    """Raised when analysis/scoring cannot produce profile-valid output."""


@dataclass(slots=True)
class DimensionScores:
    """Per-dimension fit, each on a 0-100 scale.

    These map one-to-one to :class:`~jobpilot.config.ScoringWeights`. The weighted
    overall score is their weight-average (weights sum to 100).
    """

    required_stack: float = 0.0
    seniority_and_years: float = 0.0
    modality_and_timezone: float = 0.0
    language: float = 0.0
    salary_and_conditions: float = 0.0
    other: float = 0.0


def compute_weighted_score(dimensions: DimensionScores, weights: ScoringWeights) -> int:
    """Combine per-dimension 0-100 scores into a weighted overall 0-100 score."""
    total = weights.total()
    if total <= 0:  # pragma: no cover - config loader guarantees sum == 100
        raise ScoringError("Scoring weights must sum to a positive total.")
    weighted = (
        dimensions.required_stack * weights.required_stack
        + dimensions.seniority_and_years * weights.seniority_and_years
        + dimensions.modality_and_timezone * weights.modality_and_timezone
        + dimensions.language * weights.language
        + dimensions.salary_and_conditions * weights.salary_and_conditions
        + dimensions.other * weights.other
    )
    return round(weighted / total)


def passes_threshold(score: int, min_score: int) -> bool:
    """Return whether ``score`` meets or exceeds the configured ``MIN_SCORE``."""
    return score >= min_score


def _analysis_schema_json() -> str:
    return json.dumps(JobAnalysis.model_json_schema())


def _score_schema_json() -> str:
    return json.dumps(JobScore.model_json_schema())


def analyze_job(job: Job, llm: LlmClient) -> JobAnalysis:
    """Run the analysis LLM call for a single offer."""
    prompt = build_analyze_job_prompt(
        title=job.title,
        company=job.company,
        location=job.location,
        remote=job.remote,
        salary=job.salary_text,
        description=job.description,
        schema_json=_analysis_schema_json(),
    )
    return llm.complete_json(prompt, JobAnalysis)


def score_job(
    analysis: JobAnalysis,
    profile: ProfileModel,
    llm: LlmClient,
    weights: ScoringWeights,
) -> JobScore:
    """Run the scoring LLM call for a single analysed offer."""
    prompt = build_score_job_prompt(
        profile_json=profile.model_dump_json(),
        achievement_ids=sorted(profile.achievement_ids()),
        analysis_json=analysis.model_dump_json(),
        weights=weights,
        schema_json=_score_schema_json(),
    )
    return llm.complete_json(prompt, JobScore)


def _invalid_met_ids(score: JobScore, profile: ProfileModel) -> list[str]:
    known = profile.achievement_ids()
    return [m.achievement_id for m in score.met_requirements if m.achievement_id not in known]


def analyze_and_score(
    job: Job,
    profile: ProfileModel,
    llm: LlmClient,
    config: AppConfig,
) -> tuple[JobAnalysis, JobScore]:
    """Analyse and score ``job``, validating cited achievement ids against ``profile``.

    Retries the analyse+score pair once if the score cites an unknown achievement
    id; raises :class:`ScoringError` if it still does after the retry.
    """
    weights = config.scoring.weights
    last_invalid: list[str] = []
    attempts = 2
    for attempt in range(1, attempts + 1):
        analysis = analyze_job(job, llm)
        score = score_job(analysis, profile, llm, weights)
        invalid = _invalid_met_ids(score, profile)
        if not invalid:
            return analysis, score
        last_invalid = invalid
        log.warning(
            "score_cited_unknown_achievement_ids",
            attempt=attempt,
            unknown_ids=invalid,
            job_id=job.id,
        )
    raise ScoringError(f"Scoring cited unknown achievement ids after retry: {last_invalid}")
