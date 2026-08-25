"""Schema migrations for Yorik's Postgres database.

Files live in ``migrations_pg/`` and are named ``NNN_snake_name.sql``.
Each one runs inside a single transaction and is stamped into
``schema_migrations`` on success; a failure rolls back both the schema
change and the stamp, so a fixed file re-applies cleanly.

Who runs them:

* ``scripts/bootstrap-supabase.sh`` at install / upgrade, as
  ``supabase_admin`` — the role that owns the ``docs`` schema and may
  run cluster-level statements (a handful of Phase E migrations need
  that).
* ``backend.database.init_db()`` at every startup, as the app's
  ``postgres`` role — picks up anything the bootstrap left pending.

Both share the same ``schema_migrations`` table, so neither re-runs
what the other applied.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

log = logging.getLogger("yorik.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations_pg"
MIGRATIONS_DIR_PG = MIGRATIONS_DIR  # older callers import this name

_FILENAME_RE = re.compile(r"^(\d{3,})_([a-z0-9_]+)\.(sql|py)$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def is_python(self) -> bool:
        return self.path.suffix == ".py"


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> List[Migration]:
    """Find every well-named migration file, sorted by version. Bad
    filenames are skipped with a warning — better than silently running
    them out of order. Duplicate versions are an error."""
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
            log.warning("migrations: ignoring bad filename: %s (expected NNN_name.sql)",
                        entry.name)
            continue
        out.append(Migration(version=int(m.group(1)), name=m.group(2), path=entry))
    out.sort(key=lambda mig: mig.version)
    seen: set[int] = set()
    for mig in out:
        if mig.version in seen:
            raise RuntimeError(f"duplicate migration version {mig.version} "
                               f"(latest filename: {mig.path.name})")
        seen.add(mig.version)
    return out


def _ensure_tracking_table_pg(conn) -> None:
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
    out: set[int] = set()
    for r in rows:
        out.add(int(r["version"]) if isinstance(r, dict) else int(r[0]))
    return out


def pending_pg(conn, migrations_dir: Path = MIGRATIONS_DIR) -> List[Migration]:
    done = applied_versions_pg(conn)
    return [m for m in discover(migrations_dir) if m.version not in done]


def apply_pg(conn, migration: Migration) -> None:
    """Run one migration in a transaction and stamp it. Rolls back and
    re-raises on failure so the caller's loop stops at the first bad file."""
    _ensure_tracking_table_pg(conn)
    if migration.is_python:
        raise RuntimeError(
            f"{migration.path.name}: only .sql migrations are supported in migrations_pg/."
        )
    log.info("migrations: applying %03d_%s", migration.version, migration.name)
    sql = migration.path.read_text(encoding="utf-8")
    try:
        conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) "
            "ON CONFLICT (version) DO NOTHING",
            (migration.version, migration.name),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.exception("migrations: %03d_%s FAILED — rolled back",
                      migration.version, migration.name)
        raise


def run_pending_pg(conn, migrations_dir: Path = MIGRATIONS_DIR) -> List[int]:
    """Apply every pending migration in order. Returns the versions
    applied (empty when up to date). Stops at the first failure."""
    todo = pending_pg(conn, migrations_dir)
    if not todo:
        return []
    applied: List[int] = []
    for mig in todo:
        apply_pg(conn, mig)
        applied.append(mig.version)
    log.info("migrations: applied %d new migration(s): %s", len(applied), applied)
    return applied


# Short names — the only backend is Postgres now.
applied_versions = applied_versions_pg
pending = pending_pg
apply = apply_pg
run_pending = run_pending_pg
