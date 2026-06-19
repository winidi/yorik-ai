"""Unit tests for backend.migrations — the schema-migrations runner.

Each test gets its own tmp migrations dir + tmp SQLite DB so cases
stay independent. We exercise the runner directly (no FastAPI app
needed), since the contract is just (sqlite3.Connection, Path) →
applied versions list.

Pins these invariants:
  - Discovery picks up correctly-named files, ignores junk
  - Strict ordering by version; duplicates and gaps surfaced loudly
  - Migrations run inside a transaction — a failing one rolls back
    its partial changes AND stays "pending"
  - Re-running is a no-op when up-to-date (idempotency)
  - .sql and .py migrations behave identically w.r.t. tracking
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest


def _conn():
    """Fresh in-memory DB per test — fast, isolated, no cleanup."""
    return sqlite3.connect(":memory:")


def _write_sql(d: Path, version: int, name: str, body: str) -> Path:
    p = d / f"{version:03d}_{name}.sql"
    p.write_text(textwrap.dedent(body))
    return p


def _write_py(d: Path, version: int, name: str, body: str) -> Path:
    p = d / f"{version:03d}_{name}.py"
    p.write_text(textwrap.dedent(body))
    return p


# ─── discovery ────────────────────────────────────────────────────────

def test_discover_empty_dir_returns_empty_list(tmp_path):
    from backend.migrations import discover
    assert discover(tmp_path) == []


def test_discover_skips_README_and_init(tmp_path):
    from backend.migrations import discover
    (tmp_path / "README.md").write_text("docs")
    (tmp_path / "__init__.py").write_text("")
    assert discover(tmp_path) == []


def test_discover_skips_bad_filenames(tmp_path, caplog):
    """Files that don't match NNN_name.ext are warned-and-skipped."""
    import logging
    from backend.migrations import discover
    (tmp_path / "update.sql").write_text("-- nope")
    (tmp_path / "1_foo.sql").write_text("-- too few digits")
    (tmp_path / "001-dash-name.sql").write_text("-- dashes")
    with caplog.at_level(logging.WARNING, logger="yorik.migrations"):
        assert discover(tmp_path) == []
    assert any("ignoring bad filename" in r.message for r in caplog.records)


def test_discover_returns_versions_in_order(tmp_path):
    from backend.migrations import discover
    _write_sql(tmp_path, 3, "third", "")
    _write_sql(tmp_path, 1, "first", "")
    _write_sql(tmp_path, 2, "second", "")
    versions = [m.version for m in discover(tmp_path)]
    assert versions == [1, 2, 3]


def test_discover_raises_on_duplicate_versions(tmp_path):
    """Two files with the same NNN is an authoring bug — fail loudly,
    don't silently run one of them."""
    from backend.migrations import discover
    _write_sql(tmp_path, 1, "first", "")
    _write_py(tmp_path, 1, "also_first", "def up(c): pass\n")
    with pytest.raises(RuntimeError, match="duplicate migration version 1"):
        discover(tmp_path)


# ─── apply: SQL path ──────────────────────────────────────────────────

def test_apply_sql_creates_table_and_stamps(tmp_path):
    from backend.migrations import discover, apply
    _write_sql(tmp_path, 1, "create_widgets",
               "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT);")
    [mig] = discover(tmp_path)
    conn = _conn()
    apply(conn, mig)
    # Schema change applied
    cols = [r[1] for r in conn.execute("PRAGMA table_info(widgets)").fetchall()]
    assert cols == ["id", "name"]
    # Version stamped
    rows = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert rows == [(1, "create_widgets")]


def test_apply_sql_failure_rolls_back(tmp_path):
    """If the SQL fails partway, the transaction rolls back AND the
    version stays unstamped (so a fix-up rerun re-tries cleanly)."""
    from backend.migrations import discover, apply
    _write_sql(tmp_path, 1, "broken", """
        CREATE TABLE widgets (id INTEGER PRIMARY KEY);
        INSERT INTO nonexistent_table VALUES (1);  -- explodes
    """)
    [mig] = discover(tmp_path)
    conn = _conn()
    with pytest.raises(sqlite3.OperationalError):
        apply(conn, mig)
    # widgets must NOT exist
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "widgets" not in tables
    # Not stamped
    rows = conn.execute("SELECT * FROM schema_migrations").fetchall()
    assert rows == []


# ─── apply: Python path ───────────────────────────────────────────────

def test_apply_python_calls_up_inside_transaction(tmp_path):
    from backend.migrations import discover, apply
    _write_py(tmp_path, 1, "py_create", """
        def up(conn):
            conn.execute("CREATE TABLE py_widgets (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO py_widgets (id) VALUES (1), (2), (3)")
    """)
    [mig] = discover(tmp_path)
    conn = _conn()
    apply(conn, mig)
    n = conn.execute("SELECT COUNT(*) FROM py_widgets").fetchone()[0]
    assert n == 3
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert rows == [(1,)]


def test_apply_python_missing_up_function_fails(tmp_path):
    from backend.migrations import discover, apply
    _write_py(tmp_path, 1, "no_up", "# no up() defined\n")
    [mig] = discover(tmp_path)
    conn = _conn()
    with pytest.raises(RuntimeError, match="missing `up\\(conn\\)`"):
        apply(conn, mig)
    rows = conn.execute("SELECT * FROM schema_migrations").fetchall()
    assert rows == []


def test_apply_python_exception_rolls_back(tmp_path):
    from backend.migrations import discover, apply
    _write_py(tmp_path, 1, "raises", """
        def up(conn):
            conn.execute("CREATE TABLE half_done (id INTEGER PRIMARY KEY)")
            raise ValueError("oops")
    """)
    [mig] = discover(tmp_path)
    conn = _conn()
    with pytest.raises(ValueError):
        apply(conn, mig)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "half_done" not in tables


# ─── run_pending: orchestration ──────────────────────────────────────

def test_run_pending_on_empty_db_applies_all_in_order(tmp_path):
    from backend.migrations import run_pending
    _write_sql(tmp_path, 1, "a", "CREATE TABLE a (id INT);")
    _write_sql(tmp_path, 2, "b", "CREATE TABLE b (id INT);")
    _write_sql(tmp_path, 3, "c", "CREATE TABLE c (id INT);")
    conn = _conn()
    applied = run_pending(conn, tmp_path)
    assert applied == [1, 2, 3]
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    assert rows == [(1,), (2,), (3,)]


def test_run_pending_is_idempotent(tmp_path):
    """Second call when up-to-date returns [] and doesn't re-run anything."""
    from backend.migrations import run_pending
    _write_sql(tmp_path, 1, "a", "CREATE TABLE a (id INT);")
    conn = _conn()
    assert run_pending(conn, tmp_path) == [1]
    # Second call: no-op
    assert run_pending(conn, tmp_path) == []
    # And the schema is unchanged (CREATE TABLE without IF NOT EXISTS
    # would have raised — meaning we didn't re-run)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert rows == [(1,)]


def test_run_pending_picks_up_new_migration_after_initial_apply(tmp_path):
    """Land migration 1 → apply. Land migration 2 → only 2 applies."""
    from backend.migrations import run_pending
    _write_sql(tmp_path, 1, "a", "CREATE TABLE a (id INT);")
    conn = _conn()
    assert run_pending(conn, tmp_path) == [1]
    _write_sql(tmp_path, 2, "b", "CREATE TABLE b (id INT);")
    assert run_pending(conn, tmp_path) == [2]


def test_run_pending_failure_blocks_later_migrations(tmp_path):
    """If 002 fails, 003 must NOT be applied — the DB is in an
    indeterminate state until the operator fixes 002."""
    from backend.migrations import run_pending
    _write_sql(tmp_path, 1, "a", "CREATE TABLE a (id INT);")
    _write_sql(tmp_path, 2, "broken",
               "INSERT INTO nonexistent VALUES (1);")
    _write_sql(tmp_path, 3, "c", "CREATE TABLE c (id INT);")
    conn = _conn()
    with pytest.raises(sqlite3.OperationalError):
        run_pending(conn, tmp_path)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    # Only 1 applied; 2 failed and rolled back, 3 never tried
    assert rows == [(1,)]
    # And table c was NOT created
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "c" not in tables
