"""End-to-end scoring over es/en fixtures via the scripted fake LLM."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from jobpilot.config import AppConfig, Language, LanguageConfig, Settings
from jobpilot.llm.schemas import ConfirmLanguage
from jobpilot.models import (
    Job,
    JobAnalysis,
    JobScore,
    JobStatus,
    MetRequirement,
    RawJob,
    Recommendation,
    ScoreDimensions,
)
from jobpilot.pipeline.run import score_new_jobs
from jobpilot.profile.model import ProfileModel
from tests.fakes import FakeJobRepository, ScriptedFakeLlmClient

Fixture = Callable[[str], Any]


def _persist_raw(repo: FakeJobRepository, raw: dict[str, Any]) -> Job:
    return repo.upsert_job(RawJob.model_validate(raw))


def test_score_new_jobs_es_and_en(example_profile: ProfileModel, load_fixture: Fixture) -> None:
    repo = FakeJobRepository()
    job_es = _persist_raw(repo, load_fixture("jobs_es.json"))
    job_en = _persist_raw(repo, load_fixture("jobs_en.json"))

    llm = ScriptedFakeLlmClient(
        [
            JobAnalysis.model_validate(load_fixture("llm_analysis_es.json")),
            JobScore.model_validate(load_fixture("llm_score_es.json")),
            JobAnalysis.model_validate(load_fixture("llm_analysis_en.json")),
            JobScore.model_validate(load_fixture("llm_score_en.json")),
        ]
    )
    settings = Settings(min_score=70)

    summary = score_new_jobs([job_es, job_en], repo, example_profile, llm, AppConfig(), settings)

    assert summary.scored == 2
    assert summary.below_threshold == 0
    assert summary.errors == 0

    stored_es = repo.get_job(job_es.id)  # type: ignore[arg-type]
    stored_en = repo.get_job(job_en.id)  # type: ignore[arg-type]
    assert stored_es is not None and stored_en is not None
    assert stored_es.status is JobStatus.scored
    # Source language is null in both fixtures, so es/en here can only come from
    # lingua detection in the pipeline (clear text clears the threshold, no LLM).
    assert stored_es.language is not None and stored_es.language.value == "es"
    assert stored_en.language is not None and stored_en.language.value == "en"
    assert stored_es.score == 82
    assert stored_en.score == 88


def test_low_confidence_offer_invokes_language_confirmation(
    example_profile: ProfileModel, load_fixture: Fixture
) -> None:
    """A low-confidence offer triggers the LLM confirm_language step in the pipeline."""
    repo = FakeJobRepository()
    # A terse offer gives lingua only weak signal (best guess below the 0.80
    # threshold), so the pipeline must fall back to the LLM confirmation prompt.
    raw = {
        "source": "remoteok",
        "external_id": "rok-lowconf-001",
        "url": "https://remoteok.com/remote-jobs/backend-lowconf-001",
        "title": "Backend developer Java Python",
        "company": "Terse Co",
        "remote": True,
        "language": None,
        "description": "",
    }
    job = _persist_raw(repo, raw)

    config = AppConfig(language=LanguageConfig(min_confidence=0.80))
    llm = ScriptedFakeLlmClient(
        [
            JobAnalysis.model_validate(load_fixture("llm_analysis_en.json")),
            JobScore.model_validate(load_fixture("llm_score_en.json")),
            ConfirmLanguage(language=Language.en, confidence=1.0),
        ]
    )

    summary = score_new_jobs([job], repo, example_profile, llm, config, Settings(min_score=70))
    assert summary.scored == 1
    stored = repo.get_job(job.id)  # type: ignore[arg-type]
    assert stored is not None
    assert stored.language is Language.en
    # analyse + score + confirm_language were all consumed.
    assert len(llm.prompts) == 3
    assert any("identifying the natural language" in p for p in llm.prompts)


def test_below_threshold_is_flagged(example_profile: ProfileModel, load_fixture: Fixture) -> None:
    repo = FakeJobRepository()
    job = _persist_raw(repo, load_fixture("jobs_en.json"))

    low_score = JobScore(
        # Every dimension at 40 -> computed aggregate 40 under default weights.
        dimensions=ScoreDimensions(
            required_stack=40,
            seniority_and_years=40,
            modality_and_timezone=40,
            language=40,
            salary_and_conditions=40,
            other=40,
        ),
        reasons=["weak stack match"],
        met_requirements=[],
        missing_requirements=["Java depth"],
        recommendation=Recommendation.discard,
    )
    llm = ScriptedFakeLlmClient(
        [JobAnalysis.model_validate(load_fixture("llm_analysis_en.json")), low_score]
    )

    summary = score_new_jobs([job], repo, example_profile, llm, AppConfig(), Settings(min_score=70))
    assert summary.scored == 1
    assert summary.below_threshold == 1
    stored = repo.get_job(job.id)  # type: ignore[arg-type]
    assert stored is not None and stored.status is JobStatus.scored
    assert stored.score == 40


def test_unrecoverable_llm_failure_marks_error(
    example_profile: ProfileModel, load_fixture: Fixture
) -> None:
    repo = FakeJobRepository()
    job = _persist_raw(repo, load_fixture("jobs_en.json"))

    analysis = JobAnalysis.model_validate(load_fixture("llm_analysis_en.json"))
    bad = JobScore(
        score=80,
        met_requirements=[MetRequirement(requirement="Java", achievement_id="ach_unknown")],
        recommendation=Recommendation.apply,
    )
    llm = ScriptedFakeLlmClient(
        [analysis.model_copy(), bad.model_copy(), analysis.model_copy(), bad.model_copy()]
    )

    summary = score_new_jobs([job], repo, example_profile, llm, AppConfig())
    assert summary.errors == 1
    assert summary.scored == 0
    stored = repo.get_job(job.id)  # type: ignore[arg-type]
    assert stored is not None and stored.status is JobStatus.error


def test_non_new_jobs_are_skipped(example_profile: ProfileModel, load_fixture: Fixture) -> None:
    repo = FakeJobRepository()
    job = _persist_raw(repo, load_fixture("jobs_en.json"))
    repo.set_status(job.id, JobStatus.scored)  # type: ignore[arg-type]
    already = repo.get_job(job.id)  # type: ignore[arg-type]
    assert already is not None

    llm = ScriptedFakeLlmClient()  # no responses queued; must not be called
    summary = score_new_jobs([already], repo, example_profile, llm, AppConfig())
    assert summary.scored == 0
    assert summary.analyzed == 0
    assert llm.prompts == []
