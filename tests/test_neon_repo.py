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
        if q.startswith("select id from jobs where"):
            self._lookup_job(params, cur)
        elif q.startswith("insert into jobs"):
            self._insert_job(params, cur)
        elif q.startswith("update jobs set url"):
            self._update_job(params, cur)
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

    def _lookup_job(self, params: Any, cur: FakeCursor) -> None:
        source, external_id, url = params
        # Emulate Postgres resolving the two unique constraints: match either the
        # (source, external_id) key or the url, preferring the lowest id.
        candidates = [
            job_id
            for job_id in (self.by_key.get((source, external_id)), self.by_url.get(url))
            if job_id is not None
        ]
        cur._result = {"id": min(candidates)} if candidates else None

    def _insert_job(self, params: Any, cur: FakeCursor) -> None:
        (
            source,
            external_id,
            url,
            apply_url,
            title,
            company,
            location,
            remote,
            modality,
            salary_text,
            contact_email,
            description,
            _found_at,
        ) = params
        # A faithful Postgres would raise on a constraint violation here; the
        # repository's prior lookup guarantees no existing key/url reaches this path.
        if (source, external_id) in self.by_key or url in self.by_url:
            raise psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")
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
            "source": source,
            "external_id": external_id,
            "modality": modality,
        }
        self.jobs[job_id] = row
        self._apply_descriptive(
            row,
            url=url,
            apply_url=apply_url,
            title=title,
            company=company,
            location=location,
            remote=remote,
            modality=modality,
            salary_text=salary_text,
            contact_email=contact_email,
            description=description,
        )
        self.by_key[(source, external_id)] = job_id
        self.by_url[url] = job_id
        cur._result = dict(row)

    def _update_job(self, params: Any, cur: FakeCursor) -> None:
        (
            url,
            apply_url,
            title,
            company,
            location,
            remote,
            modality,
            salary_text,
            contact_email,
            description,
            job_id,
        ) = params
        row = self.jobs[int(job_id)]
        self._apply_descriptive(
            row,
            url=url,
            apply_url=apply_url,
            title=title,
            company=company,
            location=location,
            remote=remote,
            modality=modality,
            salary_text=salary_text,
            contact_email=contact_email,
            description=description,
        )
        self.by_url[url] = int(job_id)
        cur._result = dict(row)

    @staticmethod
    def _apply_descriptive(row: dict[str, Any], **values: Any) -> None:
        row.update(values)


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


def test_different_key_same_url_updates_without_unique_violation() -> None:
    """Postgres-faithful: a different (source, external_id) with the same URL must
    resolve to the existing row via the url constraint, never raise jobs_url_key."""
    conn = FakeConnection()
    repo = NeonJobRepository(conn)

    first = repo.upsert_job(
        RawJob(source="srcA", external_id="a1", url="https://dup/1", title="T", company="C")
    )
    # A second posting from another source/id but the same canonical URL. If the
    # repository blindly INSERTed it, the FakeConnection would raise UniqueViolation.
    second = repo.upsert_job(
        RawJob(
            source="srcB",
            external_id="b1",
            url="https://dup/1",
            title="T updated",
            company="C",
        )
    )

    assert first.id == second.id
    assert first.id is not None
    assert len(conn.jobs) == 1
    assert conn.jobs[first.id]["title"] == "T updated"


def test_upsert_rolls_back_and_stays_usable_after_failure() -> None:
    conn = FakeConnection()
    repo = NeonJobRepository(conn)

    class BoomConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.rolled_back = 0

        def rollback(self) -> None:
            self.rolled_back += 1

        def execute(self, query: str, params: Any, cur: FakeCursor) -> None:
            if " ".join(query.lower().split()).startswith("select id from jobs"):
                raise psycopg.OperationalError("connection hiccup")
            super().execute(query, params, cur)

    boom = BoomConnection()
    repo = NeonJobRepository(boom)
    with pytest.raises(psycopg.OperationalError):
        repo.upsert_job(_raw())
    assert boom.rolled_back == 1


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
