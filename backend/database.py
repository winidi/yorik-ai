"""Connection helpers for Yorik's Postgres database.

Yorik runs on the Postgres that ships with the bundled Supabase stack
(infra/supabase/docker). Two logical stores live in one database:

  * schema ``public`` — everything personal: users, calendars, tasks,
    contacts, email, WhatsApp, conversations, settings …
  * schema ``docs``   — the document corpus and its pgvector embeddings
    (native uploads, the Paperless mirror, WhatsApp message vectors).

The split survives from the days when these were two SQLite files
(family.db / documents.db). Call sites still pass a "path" hint to
``conn_ctx()`` / ``get_conn()``: anything mentioning documents routes
to the docs pool, everything else to the main pool. The hint has no
file semantics any more.

The schema lives in ``migrations_pg/``. ``scripts/bootstrap-supabase.sh``
applies it at install time as ``supabase_admin``; ``init_db()`` re-runs
the pending list at every startup.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("yorik.database")

# Routing hints. Kept as module constants because ~40 call sites pass
# them through ``conn_ctx(DEFAULT_DB_PATH)``; they are not files.
DEFAULT_DB_PATH = "main"
DEFAULT_DOCS_DB_PATH = "docs"


def _schema_for_path(path: str | None) -> str:
    """Map a legacy path hint to a pool: anything mentioning documents
    (``docs``, ``documents``, ``…/documents.db``) → ``docs``; else ``main``."""
    if path and "doc" in str(path).lower():
        return "docs"
    return "main"


def get_conn(path: str | None = None):
    """A ``PgConnection`` (sqlite3-style facade over psycopg, see
    ``db_shim``) checked out of the right pool. Close it — or use it as a
    context manager — to return the slot; ``conn_ctx()`` does that for you."""
    from .database_pg import _ensure_pool
    from .db_shim import PgConnection

    pool = _ensure_pool(_schema_for_path(path))
    return PgConnection(pool.getconn(), pool=pool)


@contextmanager
def conn_ctx(path: str | None = None) -> Iterator:
    """``with conn_ctx() as c:`` — commits on clean exit, rolls back on
    exception, returns the connection to its pool either way."""
    from .database_pg import _ensure_pool
    from .db_shim import PgConnection

    pool = _ensure_pool(_schema_for_path(path))
    with pool.connection() as raw:
        wrapped = PgConnection(raw)
        try:
            yield wrapped
            raw.commit()
        except Exception:
            raw.rollback()
            raise


def get_docs_conn(path: str | None = None):
    """Connection on the ``docs`` schema (document corpus + embeddings)."""
    return get_conn(DEFAULT_DOCS_DB_PATH)


def describe() -> str:
    """``host:port/dbname`` of the configured database, for health output
    and log lines. Never includes the password."""
    from .database_pg import _build_url

    m = re.match(r"postgresql://[^@]*@([^/]+)/(.*)$", _build_url())
    return f"{m.group(1)}/{m.group(2)}" if m else "postgres"


def check_connection(timeout_s: float = 3.0) -> tuple[bool, str]:
    """Probe the database once with a short timeout.

    Startup calls this before touching the pool so a stack that is not
    up fails with one sentence naming host, port and database instead
    of a 30 s ``PoolTimeout`` traceback — and, under systemd, instead of
    a restart loop nobody can read."""
    import psycopg

    from .database_pg import _build_url

    url = _build_url()
    where = describe()
    try:
        with psycopg.connect(url, connect_timeout=max(1, int(timeout_s))) as c:
            c.execute("SELECT 1")
        return True, where
    except Exception as exc:  # noqa: BLE001
        return False, f"{where}: {type(exc).__name__}: {exc}"


def init_db(path: str | None = None) -> None:
    """Apply pending migrations from ``migrations_pg/``. Idempotent."""
    from . import migrations as _migrations
    from .database_pg import _ensure_pool

    pool = _ensure_pool("main")
    with pool.connection() as conn:
        applied = _migrations.run_pending_pg(conn)
    if applied:
        log.info("init_db: applied %d migration(s): %s", len(applied), applied)


def init_docs_db(path: str | None = None, embed_dim: int | None = None) -> None:
    """No-op kept for callers. The ``docs`` schema and its pgvector
    columns are created by ``migrations_pg/``; pgvector needs no
    per-connection setup."""
    return None


if __name__ == "__main__":
    ok, detail = check_connection()
    if not ok:
        print(f"Postgres not reachable — {detail}")
        print("Is the bundled Supabase stack up? Run: bash scripts/bootstrap-supabase.sh")
        raise SystemExit(1)
    init_db()
    print(f"Yorik database ready at {detail}")
