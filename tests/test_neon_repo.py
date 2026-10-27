"""Tests for NeonJobRepository and connect_with_retry (no real database)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from jobpilot.models import JobStatus, RawJob
from jobpilot.storage.neon import NeonJobRepository, connect_with_retry


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self._result: dict[str, Any] | None = None
        self._results: list[dict[str, Any]] = []

    def execute(self, query: str, params: object = None, /) -> None:
        self._conn.execute(query, params, self)

    def fetchone(self) -> dict[str, Any] | None:
        return self._result

    def fetchall(self) -> list[dict[str, Any]]:
        return self._results

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConnection:
    """Minimal in-memory Postgres emulation for the repository's SQL."""

    def __init__(self) -> None:
        self.jobs: dict[int, dict[str, Any]] = {}
        self.by_key: dict[tuple[str, str], int] = {}
        self.by_url: dict[str, int] = {}
        self.state: dict[str, str] = {}
        self.next_id = 1
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:  # pragma: no cover - not exercised here
        pass

    def execute(self, query: str, params: Any, cur: FakeCursor) -> None:
        q = " ".join(query.lower().split())
        if q.startswith("insert into jobs"):
            self._upsert_job(params, cur)
        elif q.startswith("select * from jobs where id"):
            cur._result = self.jobs.get(int(params[0]))
        elif q.startswith("update jobs set status"):
            status, job_id = params
            self.jobs[int(job_id)]["status"] = status
        elif q.startswith("insert into telegram_state"):
            key, value = params
            self.state[key] = value
        elif q.startswith("select value from telegram_state"):
            key = params[0]
            cur._result = {"value": self.state[key]} if key in self.state else None
        else:  # pragma: no cover - other helpers not exercised in this test
            cur._result = None

    def _upsert_job(self, params: Any, cur: FakeCursor) -> None:
        (
            source,
            external_id,
            url,
            apply_url,
            title,
            company,
            location,
            remote,
            salary_text,
            contact_email,
            description,
            _found_at,
        ) = params
        key = (source, external_id)
        job_id = self.by_key.get(key) or self.by_url.get(url)
        if job_id is None:
            job_id = self.next_id
            self.next_id += 1
            row: dict[str, Any] = {
                "id": job_id,
                "status": "new",
                "found_at": None,
                "language": None,
                "analysis": None,
                "score": None,
                "score_reason": None,
                "missing_requirements": None,
            }
            self.jobs[job_id] = row
        row = self.jobs[job_id]
        row.update(
            source=source,
            external_id=external_id,
            url=url,
            apply_url=apply_url,
            title=title,
            company=company,
            location=location,
            remote=remote,
            salary_text=salary_text,
            contact_email=contact_email,
            description=description,
        )
        self.by_key[key] = job_id
        self.by_url[url] = job_id
        cur._result = dict(row)


def _raw(external_id: str = "e1", url: str = "https://example.com/1") -> RawJob:
    return RawJob(
        source="src",
        external_id=external_id,
        url=url,
        title="Engineer",
        company="Acme",
        description="desc",
    )


def test_upsert_is_idempotent_returns_single_logical_row() -> None:
    conn = FakeConnection()
    repo = NeonJobRepository(conn)

    first = repo.upsert_job(_raw())
    second = repo.upsert_job(_raw())

    assert first.id == second.id
    assert len(conn.jobs) == 1
    assert first.id is not None


def test_url_conflict_maps_to_same_row() -> None:
    conn = FakeConnection()
    repo = NeonJobRepository(conn)

    first = repo.upsert_job(_raw(external_id="e1", url="https://x/1"))
    # Different external id but same url -> same logical job via the url index.
    second = repo.upsert_job(_raw(external_id="e2", url="https://x/1"))

    assert first.id == second.id
    assert len(conn.jobs) == 1


def test_set_status_and_state_roundtrip() -> None:
    conn = FakeConnection()
    repo = NeonJobRepository(conn)
    job = repo.upsert_job(_raw())
    assert job.id is not None

    repo.set_status(job.id, JobStatus.filtered_out)
    assert conn.jobs[job.id]["status"] == "filtered_out"

    assert repo.get_state("last_update_id") is None
    repo.set_state("last_update_id", "42")
    assert repo.get_state("last_update_id") == "42"


def test_connect_with_retry_recovers_after_operational_error() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def factory(dsn: str, **kwargs: Any) -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise psycopg.OperationalError("waking up")
        return "connection"

    conn = connect_with_retry("postgres://x", retries=5, factory=factory, sleep=sleeps.append)
    assert conn == "connection"
    assert len(attempts) == 3
    assert len(sleeps) == 2  # two backoff sleeps before the successful third try


def test_connect_with_retry_gives_up() -> None:
    def factory(dsn: str, **kwargs: Any) -> str:
        raise psycopg.OperationalError("always down")

    with pytest.raises(ConnectionError):
        connect_with_retry("postgres://x", retries=2, factory=factory, sleep=lambda _: None)
