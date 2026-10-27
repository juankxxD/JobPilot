"""Neon (Postgres) implementation of :class:`~jobpilot.interfaces.JobRepository`.

Uses psycopg 3. All SQL is parameterized. Because Neon databases can be asleep,
connections are established through :func:`connect_with_retry`, which retries
``OperationalError``/timeouts with exponential backoff (R12.1).

Tests inject a fake connection factory, so no real Postgres is contacted.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import TYPE_CHECKING, Any, Protocol

from jobpilot.config import Language
from jobpilot.logging import get_logger
from jobpilot.models import (
    Application,
    ApplicationChannel,
    ApplicationOutcome,
    Job,
    JobAnalysis,
    JobStatus,
    Modality,
    RawJob,
)

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)


class _Cursor(Protocol):
    def execute(self, query: str, params: object = ..., /) -> object: ...

    def fetchone(self) -> dict[str, Any] | None: ...

    def fetchall(self) -> list[dict[str, Any]]: ...

    def __enter__(self) -> _Cursor: ...

    def __exit__(self, *exc: object) -> object: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def connect_with_retry(
    dsn: str,
    *,
    retries: int = 5,
    base_delay: float = 1.0,
    factory: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Connect to Postgres, retrying on wake-up errors with exponential backoff.

    Neon suspends idle databases, so the first connection of a run can fail with an
    ``OperationalError`` or a timeout while the compute wakes up. We retry up to
    ``retries`` times with ``base_delay * 2**(attempt-1)`` seconds between attempts.
    """
    import psycopg

    connect = factory if factory is not None else psycopg.connect
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            conn = connect(dsn, connect_timeout=10)
            if attempt > 1:
                log.info("neon_connected", attempt=attempt)
            return conn
        except (psycopg.OperationalError, TimeoutError) as exc:
            last_error = exc
            log.warning("neon_connect_retry", attempt=attempt, error=str(exc))
            if attempt < retries:
                sleep(base_delay * (2 ** (attempt - 1)))
    raise ConnectionError(f"Could not connect to Postgres after {retries} attempts: {last_error}")


def _row_to_job(row: dict[str, Any]) -> Job:
    analysis_raw = row.get("analysis")
    analysis: JobAnalysis | None = None
    if analysis_raw is not None:
        data = analysis_raw if isinstance(analysis_raw, dict) else json.loads(analysis_raw)
        analysis = JobAnalysis.model_validate(data)

    missing_raw = row.get("missing_requirements")
    if missing_raw is None:
        missing: list[str] = []
    elif isinstance(missing_raw, list):
        missing = list(missing_raw)
    else:
        missing = list(json.loads(missing_raw))

    language = row.get("language")
    modality = row.get("modality")
    return Job(
        id=row["id"],
        source=row["source"],
        external_id=row["external_id"],
        url=row["url"],
        apply_url=row.get("apply_url"),
        title=row["title"],
        company=row["company"],
        location=row.get("location"),
        remote=bool(row.get("remote", False)),
        modality=Modality(modality) if modality else None,
        salary_text=row.get("salary_text"),
        contact_email=row.get("contact_email"),
        language=Language(language) if language else None,
        description=row.get("description") or "",
        analysis=analysis,
        score=row.get("score"),
        score_reason=row.get("score_reason"),
        missing_requirements=missing,
        status=JobStatus(row["status"]),
        found_at=row.get("found_at"),
    )


def _row_to_application(row: dict[str, Any]) -> Application:
    return Application(
        id=row["id"],
        job_id=row["job_id"],
        language=Language(row["language"]),
        cv_variant=row["cv_variant"],
        cv_url=row.get("cv_url"),
        cover_letter_url=row.get("cover_letter_url"),
        channel=ApplicationChannel(row["channel"]),
        applied_at=row.get("applied_at"),
        follow_up_at=row.get("follow_up_at"),
        outcome=ApplicationOutcome(row["outcome"]),
        notes=row.get("notes"),
        created_at=row.get("created_at"),
    )


class NeonJobRepository:
    """Postgres-backed repository implementing the ``JobRepository`` Protocol."""

    def __init__(self, conn: _Connection) -> None:
        self._conn = conn

    @classmethod
    def connect(cls, dsn: str, *, retries: int = 5) -> NeonJobRepository:
        """Open a retrying connection (dict rows) and wrap it in a repository."""
        from psycopg.rows import dict_row

        conn = connect_with_retry(dsn, retries=retries)
        conn.row_factory = dict_row
        return cls(conn)

    # ------------------------------------------------------------------ #
    # JobRepository Protocol
    # ------------------------------------------------------------------ #
    def upsert_job(self, job: RawJob) -> Job:
        """Idempotent, atomic upsert resolving conflicts on *either* unique identity.

        The ``jobs`` table has two unique constraints: ``(source, external_id)`` and
        ``url``. A ``SELECT`` followed by a plain ``INSERT`` is not safe: two
        collectors running concurrently can both observe no existing row and then
        race to insert, so one raises a unique violation and aborts collection.

        Instead we rely on Postgres' own conflict handling. The primary path is a
        single atomic ``INSERT ... ON CONFLICT (source, external_id) DO UPDATE ...
        RETURNING``, which both inserts a brand-new posting and refreshes an existing
        one in one statement with no lost-update window. A single ``ON CONFLICT``
        clause can only name one constraint, so if a *different* ``(source,
        external_id)`` carries an already-seen ``url`` (a relisted or cross-posted
        job) the statement raises ``jobs_url_key``. We handle that by rolling back
        the aborted statement and running an equally atomic ``UPDATE ... WHERE url =
        %s RETURNING``, which updates the existing row in place. Either way exactly
        one logical row survives and collection is never aborted.

        On update the mutable descriptive columns are refreshed, but the job's id and
        status are preserved, and ``language`` is only *filled* when still NULL
        (``COALESCE``) so a language established later by analysis is never
        overwritten, while any analysis/score already made is left untouched.
        """
        import psycopg

        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jobs "
                    "(source, external_id, url, apply_url, title, company, location, "
                    " remote, modality, salary_text, contact_email, language, "
                    " description, found_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    " COALESCE(%s, now())) "
                    "ON CONFLICT (source, external_id) DO UPDATE SET "
                    " url = EXCLUDED.url, apply_url = EXCLUDED.apply_url, "
                    " title = EXCLUDED.title, company = EXCLUDED.company, "
                    " location = EXCLUDED.location, remote = EXCLUDED.remote, "
                    " modality = EXCLUDED.modality, salary_text = EXCLUDED.salary_text, "
                    " contact_email = EXCLUDED.contact_email, "
                    " language = COALESCE(jobs.language, EXCLUDED.language), "
                    " description = EXCLUDED.description "
                    "RETURNING *",
                    self._insert_params(job),
                )
                row = cur.fetchone()
            self._conn.commit()
        except psycopg.errors.UniqueViolation:
            # The new (source, external_id) collided on the url constraint instead.
            # Roll back the aborted statement and update the existing row by url.
            self._conn.rollback()
            row = self._update_by_url(job)
            self._conn.commit()
        except Exception:
            # Leave the connection in a usable state for the next statement so a
            # single failed upsert does not abort the rest of collection.
            self._conn.rollback()
            raise
        if row is None:  # pragma: no cover - RETURNING always yields a row
            raise RuntimeError("upsert_job did not return a row")
        return _row_to_job(row)

    @staticmethod
    def _insert_params(job: RawJob) -> tuple[Any, ...]:
        return (
            job.source,
            job.external_id,
            job.url,
            job.apply_url,
            job.title,
            job.company,
            job.location,
            job.remote,
            job.modality.value if job.modality is not None else None,
            job.salary_text,
            job.contact_email,
            job.language.value if job.language is not None else None,
            job.description,
            job.found_at,
        )

    def _update_by_url(self, job: RawJob) -> dict[str, Any] | None:
        """Atomically update the row that already owns ``job.url`` (url-conflict path).

        The ``(source, external_id)`` of the existing row is kept (the colliding new
        identity is discarded); ``language`` is only filled when still NULL so later
        analysis is never regressed.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET "
                " apply_url = %s, title = %s, company = %s, location = %s, "
                " remote = %s, modality = %s, salary_text = %s, contact_email = %s, "
                " language = COALESCE(language, %s), description = %s "
                "WHERE url = %s RETURNING *",
                (
                    job.apply_url,
                    job.title,
                    job.company,
                    job.location,
                    job.remote,
                    job.modality.value if job.modality is not None else None,
                    job.salary_text,
                    job.contact_email,
                    job.language.value if job.language is not None else None,
                    job.description,
                    job.url,
                ),
            )
            return cur.fetchone()

    def get_job(self, job_id: int) -> Job | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        return _row_to_job(row) if row is not None else None

    def set_status(self, job_id: int, status: JobStatus) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status = %s WHERE id = %s",
                (status.value, job_id),
            )
        self._conn.commit()

    def save_job(self, job: Job) -> Job:
        """Persist analysis/score/status fields of an existing job."""
        if job.id is None:
            raise ValueError("save_job requires a persisted job with an id")
        analysis_json = job.analysis.model_dump_json() if job.analysis is not None else None
        missing_json = json.dumps(job.missing_requirements)
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET "
                " language = %s, modality = %s, analysis = %s, score = %s, "
                " score_reason = %s, missing_requirements = %s, status = %s "
                "WHERE id = %s RETURNING *",
                (
                    job.language.value if job.language else None,
                    job.modality.value if job.modality else None,
                    analysis_json,
                    job.score,
                    job.score_reason,
                    missing_json,
                    job.status.value,
                    job.id,
                ),
            )
            row = cur.fetchone()
        self._conn.commit()
        if row is None:
            raise ValueError(f"save_job: job {job.id} does not exist")
        return _row_to_job(row)

    def create_application(self, app: Application) -> Application:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applications "
                "(job_id, language, cv_variant, cv_url, cover_letter_url, channel, "
                " applied_at, follow_up_at, outcome, notes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (
                    app.job_id,
                    app.language.value,
                    app.cv_variant,
                    str(app.cv_url) if app.cv_url is not None else None,
                    str(app.cover_letter_url) if app.cover_letter_url is not None else None,
                    app.channel.value,
                    app.applied_at,
                    app.follow_up_at,
                    app.outcome.value,
                    app.notes,
                ),
            )
            row = cur.fetchone()
        self._conn.commit()
        if row is None:  # pragma: no cover - RETURNING always yields a row
            raise RuntimeError("create_application did not return a row")
        return _row_to_application(row)

    def get_state(self, key: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT value FROM telegram_state WHERE key = %s", (key,))
            row = cur.fetchone()
        if row is None:
            return None
        value = row["value"]
        return str(value) if value is not None else None

    def set_state(self, key: str, value: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telegram_state (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Query helpers used by later phases (A3) — not part of the Protocol.
    # ------------------------------------------------------------------ #
    def list_proposed(self) -> list[Job]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE status = %s ORDER BY id", ("proposed",))
            rows = cur.fetchall()
        return [_row_to_job(row) for row in rows]

    def recent_company_role(self, company: str, role: str, within_days: int) -> bool:
        """True if a job for the same company+role was proposed within the window."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM jobs "
                "WHERE lower(company) = lower(%s) AND lower(title) = lower(%s) "
                "  AND status IN ('proposed', 'approved', 'applied') "
                "  AND found_at >= now() - make_interval(days => %s) "
                "LIMIT 1",
                (company, role, within_days),
            )
            return cur.fetchone() is not None

    def count_applications_on(self, day: dt.date) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM applications WHERE applied_at::date = %s",
                (day,),
            )
            row = cur.fetchone()
        return int(row["n"]) if row is not None else 0

    def applications_without_outcome(self, before: dt.datetime) -> list[Application]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM applications "
                "WHERE outcome = 'pending' AND follow_up_at IS NOT NULL "
                "  AND follow_up_at <= %s "
                "ORDER BY follow_up_at",
                (before,),
            )
            rows = cur.fetchall()
        return [_row_to_application(row) for row in rows]
