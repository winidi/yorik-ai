"""Unit tests for backend.migrations — the schema-migrations runner.

Discovery tests use a tmp directory of migration files. Apply tests run
against an EMPTY throwaway Postgres database (see conftest.pg_scratch_conn)
so the contract under test is the one production runs: one transaction
per file, stamped on success, rolled back and left pending on failure.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import psycopg
import pytest


def _write_sql(d: Path, version: int, name: str, body: str) -> Path:
    p = d / f"{version:03d}_{name}.sql"
    p.write_text(textwrap.dedent(body))
    return p


def _tables(conn) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    }


# ─── discovery ────────────────────────────────────────────────────────

def test_discover_empty_dir_returns_empty_list(tmp_path):
    from backend.migrations import discover
    assert discover(tmp_path) == []


def test_discover_missing_dir_returns_empty_list(tmp_path):
    from backend.migrations import discover
    assert discover(tmp_path / "nope") == []


def test_discover_skips_README_and_init(tmp_path):
    from backend.migrations import discover
    (tmp_path / "README.md").write_text("docs")
    (tmp_path / "__init__.py").write_text("")
    _write_sql(tmp_path, 1, "a", "SELECT 1;")
    assert [m.version for m in discover(tmp_path)] == [1]


def test_discover_skips_bad_filenames(tmp_path, caplog):
    from backend.migrations import discover
    (tmp_path / "notes.sql").write_text("SELECT 1;")
    (tmp_path / "1_no_padding.sql").write_text("SELECT 1;")
    _write_sql(tmp_path, 2, "good", "SELECT 1;")
    assert [m.version for m in discover(tmp_path)] == [2]
    assert "ignoring bad filename" in caplog.text


def test_discover_returns_versions_in_order(tmp_path):
    from backend.migrations import discover
    _write_sql(tmp_path, 3, "c", "SELECT 1;")
    _write_sql(tmp_path, 1, "a", "SELECT 1;")
    _write_sql(tmp_path, 2, "b", "SELECT 1;")
    assert [m.version for m in discover(tmp_path)] == [1, 2, 3]


def test_discover_raises_on_duplicate_versions(tmp_path):
    from backend.migrations import discover
    _write_sql(tmp_path, 1, "a", "SELECT 1;")
    _write_sql(tmp_path, 1, "b", "SELECT 1;")
    with pytest.raises(RuntimeError, match="duplicate migration version 1"):
        discover(tmp_path)


# ─── apply ────────────────────────────────────────────────────────────

def test_apply_sql_creates_table_and_stamps(tmp_path, pg_scratch_conn):
    from backend.migrations import discover, apply_pg
    _write_sql(tmp_path, 1, "create_widgets",
               "CREATE TABLE widgets (id BIGSERIAL PRIMARY KEY, name TEXT);")
    [mig] = discover(tmp_path)
    apply_pg(pg_scratch_conn, mig)
    assert "widgets" in _tables(pg_scratch_conn)
    rows = pg_scratch_conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert rows == [(1, "create_widgets")]


def test_apply_sql_failure_rolls_back(tmp_path, pg_scratch_conn):
    """If the SQL fails partway, the transaction rolls back AND the
    version stays unstamped (so a fixed file re-applies cleanly)."""
    from backend.migrations import discover, apply_pg
    _write_sql(tmp_path, 1, "broken", """
        CREATE TABLE widgets (id BIGSERIAL PRIMARY KEY);
        INSERT INTO nonexistent_table VALUES (1);
    """)
    [mig] = discover(tmp_path)
    with pytest.raises(psycopg.errors.UndefinedTable):
        apply_pg(pg_scratch_conn, mig)
    assert "widgets" not in _tables(pg_scratch_conn)
    assert pg_scratch_conn.execute("SELECT * FROM schema_migrations").fetchall() == []


def test_apply_rejects_python_migrations(tmp_path, pg_scratch_conn):
    from backend.migrations import discover, apply_pg
    (tmp_path / "001_py.py").write_text("def up(conn): pass\n")
    [mig] = discover(tmp_path)
    with pytest.raises(RuntimeError, match="only .sql migrations"):
        apply_pg(pg_scratch_conn, mig)


# ─── run_pending ──────────────────────────────────────────────────────

def test_run_pending_on_empty_db_applies_all_in_order(tmp_path, pg_scratch_conn):
    from backend.migrations import run_pending_pg
    _write_sql(tmp_path, 1, "a", "CREATE TABLE a (x INT);")
    _write_sql(tmp_path, 2, "b", "CREATE TABLE b (x INT);")
    assert run_pending_pg(pg_scratch_conn, tmp_path) == [1, 2]
    assert {"a", "b"} <= _tables(pg_scratch_conn)


def test_run_pending_is_idempotent(tmp_path, pg_scratch_conn):
    from backend.migrations import run_pending_pg
    _write_sql(tmp_path, 1, "a", "CREATE TABLE a (x INT);")
    assert run_pending_pg(pg_scratch_conn, tmp_path) == [1]
    assert run_pending_pg(pg_scratch_conn, tmp_path) == []


def test_run_pending_picks_up_new_migration_after_initial_apply(tmp_path, pg_scratch_conn):
    from backend.migrations import run_pending_pg
    _write_sql(tmp_path, 1, "a", "CREATE TABLE a (x INT);")
    run_pending_pg(pg_scratch_conn, tmp_path)
    _write_sql(tmp_path, 2, "b", "CREATE TABLE b (x INT);")
    assert run_pending_pg(pg_scratch_conn, tmp_path) == [2]


def test_run_pending_failure_blocks_later_migrations(tmp_path, pg_scratch_conn):
    from backend.migrations import run_pending_pg
    _write_sql(tmp_path, 1, "a", "CREATE TABLE a (x INT);")
    _write_sql(tmp_path, 2, "broken", "INSERT INTO nonexistent_table VALUES (1);")
    _write_sql(tmp_path, 3, "c", "CREATE TABLE c (x INT);")
    with pytest.raises(psycopg.errors.UndefinedTable):
        run_pending_pg(pg_scratch_conn, tmp_path)
    tables = _tables(pg_scratch_conn)
    assert "a" in tables and "c" not in tables
    stamped = {r[0] for r in pg_scratch_conn.execute("SELECT version FROM schema_migrations").fetchall()}
    assert stamped == {1}


def test_shipped_migrations_discover_cleanly():
    """The real migrations_pg/ directory parses with no gaps or duplicates."""
    from backend.migrations import MIGRATIONS_DIR, discover
    versions = [m.version for m in discover(MIGRATIONS_DIR)]
    assert versions and versions == sorted(versions)
    assert all(m.path.suffix == ".sql" for m in discover(MIGRATIONS_DIR))
