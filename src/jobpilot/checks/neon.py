"""Verify the Neon (Postgres) credential with a trivial query.

Neon may be asleep, so the connection is retried a few times.
"""

from __future__ import annotations

import time

from jobpilot.checks.base import CheckResult, missing
from jobpilot.config import get_settings


def check_neon(retries: int = 3, backoff: float = 2.0) -> CheckResult:
    settings = get_settings()
    if not settings.database_url:
        return missing("Neon", "DATABASE_URL")

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dependency guard
        return CheckResult("Neon", False, f"psycopg not installed: {exc}")

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with (
                psycopg.connect(settings.database_url, connect_timeout=10) as conn,
                conn.cursor() as cur,
            ):
                cur.execute("SELECT 1")
                cur.fetchone()
            return CheckResult("Neon", True, f"connected on attempt {attempt}")
        except Exception as exc:  # noqa: BLE001 - Neon may be waking up
            last_error = exc
            if attempt < retries:
                time.sleep(backoff * attempt)
    return CheckResult("Neon", False, f"connection failed after {retries} attempts: {last_error}")
