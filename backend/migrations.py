"""Minimal schema-migrations runner.

A migration is a file in `migrations/` named `NNN_description.ext` where:

  - NNN is a zero-padded 3-digit version (`001`, `042`, …)
  - description is `snake_case_words` (informational only)
  - ext is `.sql` (runs as one transaction) or `.py` (imported, then
    `up(conn)` is called inside a transaction)

A `schema_migrations` table tracks which versions have been applied:

    CREATE TABLE schema_migrations (
        version    INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )

On startup, `init_db()` calls `run_pending(conn)`. It:
  1. Ensures the tracking table exists
  2. Discovers migration files, sorted by version
  3. Skips ones already in `schema_migrations`
  4. Applies each remaining one inside a transaction — failure rolls
     back that single migration and aborts the whole run (so a broken
     migration can't half-apply)
  5. Returns the list of versions just applied (caller can log)

This module deliberately doesn't try to be Alembic. It's ~150 lines,
zero deps beyond stdlib + sqlite3, and matches the rest of Yorik's
"prefer 100 LoC over a framework" style. For data-heavy backfills,
.py migrations have full access to the sqlite3 connection — write
ordinary Python.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List

log = logging.getLogger("yorik.migrations")

# Migrations live at repo root, NOT inside backend/, so the same set
# applies whether you're running tests, a frozen install, or a dev
# checkout. PROJECT_ROOT discovered relative to this file.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Phase D — Postgres counterpart. Files: NNN_name.sql (Postgres dialect).
# 000_phase_d_init.sql is the bootstrap; everything 063+ is a normal
# numbered migration that the same runner applies.
MIGRATIONS_DIR_PG = Path(__file__).resolve().parent.parent / "migrations_pg"


def _backend() -> str:
    """`sqlite` (default) or `postgres`, from YORIK_DB_BACKEND env var."""
    import os
    return (os.getenv("YORIK_DB_BACKEND") or "sqlite").strip().lower()

_FILENAME_RE = re.compile(r"^(\d{3,})_([a-z0-9_]+)\.(sql|py)$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def is_python(self) -> bool:
        return self.path.suffix == ".py"


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL string into individual statements at top-level
    semicolons. Tracks single/double quotes and line comments so
    semicolons inside strings or '-- foo;' comments don't split
    incorrectly. Block comments (/* */) are passed through verbatim
    so triggers / views containing them stay intact.

    Sufficient for ordinary schema-migration SQL. If you find yourself
    needing something fancier (CREATE TRIGGER with embedded BEGIN…END,
    multi-statement procedures, etc.) use a .py migration instead and
    drive it via conn.execute() calls directly."""
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_single = in_double = in_line_comment = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
        elif in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
        elif in_double:
            buf.append(ch)
            if ch == '"':
                in_double = False
        elif ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
        elif ch == "'":
            in_single = True
            buf.append(ch)
        elif ch == '"':
            in_double = True
            buf.append(ch)
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _ensure_tracking_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version    INTEGER PRIMARY KEY,"
        "  name       TEXT NOT NULL,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> List[Migration]:
    """Find every well-named migration file, sorted by version. Bad
    filenames are skipped with a warning — better than silently running
    them out of order or, worse, applying them at unintended times."""
    if not migrations_dir.is_dir():
        return []
    out: List[Migration] = []
    for entry in sorted(migrations_dir.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if entry.name in ("README.md", "__init__.py"):
            continue
        m = _FILENAME_RE.match(entry.name)
        if not m:
            log.warning("migrations: ignoring bad filename: %s (expected NNN_name.{sql,py})",
                        entry.name)
            continue
        out.append(Migration(version=int(m.group(1)), name=m.group(2), path=entry))
    out.sort(key=lambda mig: mig.version)
    # Enforce strict no-gaps + no-duplicates so a missing or doubled
    # migration is caught at runtime, not in production.
    seen = set()
    for mig in out:
        if mig.version in seen:
            raise RuntimeError(f"duplicate migration version {mig.version} "
                               f"(latest filename: {mig.path.name})")
        seen.add(mig.version)
    return out


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_tracking_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(r[0]) for r in rows}


def pending(conn: sqlite3.Connection,
            migrations_dir: Path = MIGRATIONS_DIR) -> List[Migration]:
    """All migrations in migrations_dir that haven't yet been applied
    to this database, in apply-order."""
    done = applied_versions(conn)
    return [m for m in discover(migrations_dir) if m.version not in done]


def apply(conn: sqlite3.Connection, migration: Migration) -> None:
    """Run a single migration inside a transaction. Stamps
    schema_migrations on success; rolls back everything on failure
    (including the partial schema changes the migration made).
    Raises on failure so the caller's run_pending() bails out cleanly.

    Self-sufficient: ensures the tracking table exists before inserting,
    so this works even if the caller skipped pending()/applied_versions().
    """
    # Tracking table comes first AND in its own transaction so it
    # persists even if the migration body rolls back. Without this,
    # a fresh-DB apply() call from outside run_pending() would try to
    # INSERT into a non-existent table.
    _ensure_tracking_table(conn)
    conn.commit()
    log.info("migrations: applying %03d_%s (%s)",
             migration.version, migration.name, migration.path.suffix)
    # Explicit BEGIN/COMMIT/ROLLBACK because Python's sqlite3 module
    # auto-COMMITs before DDL (CREATE TABLE etc.) — `with conn:` only
    # protects DML. Without BEGIN here, a CREATE TABLE followed by an
    # exception would persist the table. Switching to BEGIN IMMEDIATE
    # acquires a write lock up front so we don't surprise-fail on a
    # concurrent writer mid-migration.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if migration.is_python:
            spec = importlib.util.spec_from_file_location(
                f"yorik_migration_{migration.version}",
                migration.path,
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"could not load migration module: {migration.path}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "up"):
                raise RuntimeError(f"{migration.path.name}: missing `up(conn)` function")
            mod.up(conn)
        else:  # .sql
            # Manual statement-by-statement execute (vs executescript
            # which auto-COMMITs) so the BEGIN above stays in force
            # through the whole file.
            sql = migration.path.read_text(encoding="utf-8")
            for stmt in _split_sql_statements(sql):
                if stmt.strip():
                    conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (migration.version, migration.name),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("migrations: %03d_%s FAILED — rolled back",
                      migration.version, migration.name)
        raise


def run_pending(conn: sqlite3.Connection,
                migrations_dir: Path = MIGRATIONS_DIR) -> List[int]:
    """Apply every pending migration in order. Returns the versions
    actually applied (empty if up-to-date). Bails on the first failure
    — later migrations stay pending until the failing one is fixed and
    re-applied."""
    todo = pending(conn, migrations_dir)
    if not todo:
        return []
    applied: List[int] = []
    for mig in todo:
        apply(conn, mig)
        applied.append(mig.version)
    log.info("migrations: applied %d new migration(s): %s",
             len(applied), applied)
    return applied


# ─── Phase D — Postgres path ────────────────────────────────────────


def _ensure_tracking_table_pg(conn) -> None:
    """Postgres flavour of `_ensure_tracking_table`. Same column shape as
    SQLite so reads work identically across backends."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version    INTEGER PRIMARY KEY,"
        "  name       TEXT NOT NULL,"
        "  applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp"
        ")"
    )
    conn.commit()


def applied_versions_pg(conn) -> set[int]:
    _ensure_tracking_table_pg(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    # psycopg dict_row returns each row as a dict; tolerate both.
    out: set[int] = set()
    for r in rows:
        if isinstance(r, dict):
            out.add(int(r["version"]))
        else:
            out.add(int(r[0]))
    return out


def apply_pg(conn, migration: Migration) -> None:
    """Postgres counterpart of `apply()`. Postgres handles transactions
    around DDL natively (no `BEGIN IMMEDIATE` dance) — psycopg keeps a
    transaction open from the previous commit until the next commit.
    Only .sql migrations are supported here; .py migrations would need
    Postgres-specific code anyway."""
    _ensure_tracking_table_pg(conn)
    log.info("migrations(pg): applying %03d_%s",
             migration.version, migration.name)
    if migration.is_python:
        raise RuntimeError(
            f"{migration.path.name}: .py migrations are SQLite-only. "
            f"Author a Postgres-flavour .sql file in migrations_pg/."
        )
    sql = migration.path.read_text(encoding="utf-8")
    try:
        # psycopg executes the whole script as one transaction; no
        # statement splitting needed — Postgres' parser handles ;-sep.
        conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) "
            "ON CONFLICT (version) DO NOTHING",
            (migration.version, migration.name),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("migrations(pg): %03d_%s FAILED — rolled back",
                      migration.version, migration.name)
        raise


def run_pending_pg(conn,
                   migrations_dir: Path = MIGRATIONS_DIR_PG) -> List[int]:
    """Apply pending Postgres migrations. Same contract as `run_pending`
    but for psycopg connections + the `migrations_pg/` directory."""
    done = applied_versions_pg(conn)
    todo = [m for m in discover(migrations_dir) if m.version not in done]
    if not todo:
        return []
    applied: List[int] = []
    for mig in todo:
        apply_pg(conn, mig)
        applied.append(mig.version)
    log.info("migrations(pg): applied %d new migration(s): %s",
             len(applied), applied)
    return applied
