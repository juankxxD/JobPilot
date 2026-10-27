"""SQL migration runner.

Applies the versioned ``migrations/NNN_*.sql`` files in order and records each
applied version in ``schema_migrations`` so re-running is a no-op (R12.2).

The runner is written against the small subset of the DB-API that psycopg 3
exposes (``cursor()``, ``execute``, ``fetchone``, ``commit``), so tests can inject
an in-memory fake connection without contacting a real Postgres.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from jobpilot.config import PROJECT_ROOT
from jobpilot.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

DEFAULT_MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

# Migration files are named like ``001_init.sql``; the leading number is the version.
_MIGRATION_RE = re.compile(r"^(?P<version>\d+)_.*\.sql$")


class _Cursor(Protocol):
    def execute(self, query: str, params: object = ..., /) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...

    def __enter__(self) -> _Cursor: ...

    def __exit__(self, *exc: object) -> object: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


def _ensure_schema_migrations(conn: _Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
    conn.commit()


def _applied_versions(conn: _Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        rows: set[str] = set()
        while True:
            row = cur.fetchone()
            if row is None:
                break
            rows.add(str(row[0]))
    return rows


def discover_migrations(migrations_dir: Path) -> list[tuple[str, Path]]:
    """Return ``(version, path)`` pairs sorted by version for every migration file."""
    found: list[tuple[str, Path]] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_RE.match(path.name)
        if match is None:
            log.warning("migration_file_ignored", file=path.name)
            continue
        found.append((match.group("version"), path))
    found.sort(key=lambda item: item[0])
    return found


def run_migrations(
    conn_factory: Callable[[], _Connection],
    migrations_dir: Path | str = DEFAULT_MIGRATIONS_DIR,
) -> list[str]:
    """Apply every not-yet-recorded migration in order.

    Each migration runs inside its own transaction; its version is recorded in the
    same transaction so a crash never leaves a half-applied migration marked done.

    Returns the list of versions applied during this call (empty when up to date).
    """
    migrations_dir = Path(migrations_dir)
    conn = conn_factory()
    _ensure_schema_migrations(conn)
    already = _applied_versions(conn)

    applied: list[str] = []
    for version, path in discover_migrations(migrations_dir):
        if version in already:
            log.debug("migration_skip", version=version)
            continue
        sql = path.read_text(encoding="utf-8")
        log.info("migration_apply", version=version, file=path.name)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            log.error("migration_failed", version=version, file=path.name)
            raise
        applied.append(version)

    log.info("migrations_done", applied=applied)
    return applied
