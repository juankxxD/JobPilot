"""Tests for the SQL migration runner (no real database)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobpilot.storage.migrate import discover_migrations, run_migrations


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self._result: list[tuple[object, ...]] = []

    def execute(self, query: str, params: object = None, /) -> None:
        self._conn.executed.append((query, params))
        q = query.strip().lower()
        if q.startswith("select version from schema_migrations"):
            self._result = [(v,) for v in sorted(self._conn.applied)]
        elif q.startswith("insert into schema_migrations"):
            assert isinstance(params, tuple)
            self._conn.applied.add(str(params[0]))
        else:
            self._result = []

    def fetchone(self) -> tuple[object, ...] | None:
        return self._result.pop(0) if self._result else None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.applied: set[str] = set()
        self.executed: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    (tmp_path / "001_init.sql").write_text("CREATE TABLE a (id int);", encoding="utf-8")
    (tmp_path / "002_extra.sql").write_text("CREATE TABLE b (id int);", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    return tmp_path


def test_discover_migrations_sorted(migrations_dir: Path) -> None:
    found = discover_migrations(migrations_dir)
    assert [version for version, _ in found] == ["001", "002"]


def test_run_migrations_applies_all_then_idempotent(migrations_dir: Path) -> None:
    conn = FakeConnection()

    applied_first = run_migrations(lambda: conn, migrations_dir)
    assert applied_first == ["001", "002"]
    assert conn.applied == {"001", "002"}

    applied_second = run_migrations(lambda: conn, migrations_dir)
    assert applied_second == []
    assert conn.applied == {"001", "002"}


def test_run_migrations_project_sql_parses(project_root: Path) -> None:
    conn = FakeConnection()
    applied = run_migrations(lambda: conn, project_root / "migrations")
    assert "001" in applied
    # The real init SQL must have been executed as one statement.
    assert any("create table" in q.lower() for q, _ in conn.executed)
