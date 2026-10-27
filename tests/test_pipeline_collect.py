"""Tests for the Phase 2 collect -> dedupe -> persist -> prefilter pipeline."""

from __future__ import annotations

from jobpilot.config import AppConfig, Language, PrefilterConfig
from jobpilot.models import JobStatus, RawJob
from jobpilot.pipeline.run import collect_and_persist
from tests.fakes import FakeJobRepository, FakeJobSource


def _raw(external_id: str, url: str, *, description: str = "java backend") -> RawJob:
    return RawJob(
        source="src",
        external_id=external_id,
        url=url,
        title="Backend Engineer",
        company="Acme",
        description=description,
        remote=True,
    )


def _config() -> AppConfig:
    return AppConfig(
        prefilter=PrefilterConfig(
            countries=["Colombia"],
            remote_ok=True,
            required_keywords=["java", "python"],
            excluded_keywords=["unpaid"],
            accepted_languages=[Language.es, Language.en],
            modalities=[],
            min_salary_usd=0,
        )
    )


def test_failing_source_is_isolated() -> None:
    repo = FakeJobRepository()
    good = FakeJobSource("good", [_raw("e1", "https://x/1")])
    bad = FakeJobSource("bad", error=RuntimeError("down"))
    other = FakeJobSource("other", [_raw("e2", "https://x/2")])

    summary = collect_and_persist([good, bad, other], repo, _config())

    assert "bad" in summary.source_errors
    assert summary.fetched == 2  # good + other, bad contributed nothing
    assert len(repo.jobs) == 2


def test_running_twice_does_not_duplicate() -> None:
    repo = FakeJobRepository()
    source = FakeJobSource("src", [_raw("e1", "https://x/1"), _raw("e2", "https://x/2")])

    collect_and_persist([source], repo, _config())
    first_count = len(repo.jobs)
    # Second run with the same payloads must not add new rows.
    collect_and_persist(
        [FakeJobSource("src", [_raw("e1", "https://x/1"), _raw("e2", "https://x/2")])],
        repo,
        _config(),
    )

    assert first_count == 2
    assert len(repo.jobs) == 2


def test_dedupe_by_url_across_sources() -> None:
    repo = FakeJobRepository()
    a = FakeJobSource("a", [_raw("e1", "https://same/1")])
    b = FakeJobSource("b", [_raw("e2", "https://same/1")])  # same url, different id

    collect_and_persist([a, b], repo, _config())
    assert len(repo.jobs) == 1


def test_prefiltered_jobs_get_filtered_out() -> None:
    repo = FakeJobRepository()
    keep = _raw("keep", "https://x/keep", description="java backend")
    drop = _raw("drop", "https://x/drop", description="ruby on rails only")
    source = FakeJobSource("src", [keep, drop])

    summary = collect_and_persist([source], repo, _config())

    statuses = {job.external_id: job.status for job in repo.jobs}
    assert statuses["keep"] == JobStatus.new
    assert statuses["drop"] == JobStatus.filtered_out
    assert summary.kept_new == 1
    assert summary.filtered_out == 1
