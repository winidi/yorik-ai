"""Postgres connection helper for Phase D — Supabase migration.

Mirrors `backend/database.py`'s `conn_ctx()` shape so call sites can
switch backends by flipping `YORIK_DB_BACKEND=postgres` once the
schema + data are in place. The dispatcher in `database.py` itself
will route to this module when the env var is set.

Two pools are kept (one per logical DB) so we preserve the historic
`family.db` / `documents.db` separation without forcing every call
site to pass a schema name. In Postgres land the split is by schema
(`public` for family, `docs` for the embedding store) rather than by
separate files.

Connection URL precedence:
  1. `YORIK_DB_URL` — full DSN, used as-is. Recommended for production.
  2. Otherwise build from `YORIK_DB_{HOST,PORT,USER,PASSWORD,NAME}`
     env vars; matches Supabase's `.env` keys + the host-side port we
     publish via `infra/supabase/docker/docker-compose.yorik.yml`
     (5435 on workstation; user overrides if they reuse the port).

Pool sizing:
  Default `min=1, max=10` per pool. Yorik is a single uvicorn process
  with 1 worker today, so we don't need a giant pool. Two pools = 20
  connections max; well under Supabase's default 100-connection limit.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal, Optional

import psycopg
from psycopg_pool import ConnectionPool

Schema = Literal["main", "docs"]


def _row_factory():
    """Use the shim's hybrid row (supports both row['col'] and row[0])
    so Yorik's mixed access styles work transparently. Imported lazily
    to avoid a circular import — backend.db_shim itself imports
    nothing from this module."""
    from .db_shim import _hybrid_row_factory
    return _hybrid_row_factory


def _read_supabase_postgres_password() -> str:
    """Last-ditch fallback: read POSTGRES_PASSWORD out of the bundled
    Supabase .env. Without this, the operator has to manually copy
    the password from infra/supabase/docker/.env into config.env as
    YORIK_DB_PASSWORD before the FastAPI pool can connect — a step
    install.sh doesn't do for them on first install, and which the
    runbook makes look optional. backup.py already does the same
    lookup; we keep it loose-coupled (read on each call only when
    the env var is empty) to avoid caching a stale value if the
    operator rotates Supabase's password mid-flight.
    """
    env_file = (Path(__file__).resolve().parent.parent
                / "infra" / "supabase" / "docker" / ".env")
    if not env_file.exists():
        return ""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("POSTGRES_PASSWORD="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _build_url(name_override: Optional[str] = None) -> str:
    """Resolve the Postgres DSN.

    `YORIK_DB_URL` wins outright if set. Otherwise fall back to the
    per-component env vars so first-run installs can copy from
    `infra/supabase/docker/.env` without crafting a DSN string.

    If YORIK_DB_PASSWORD itself isn't set (the common first-install
    case), fall back to reading POSTGRES_PASSWORD straight from
    infra/supabase/docker/.env — the same file the docker compose
    consumed at stack-up time, so the password is by construction
    correct.
    """
    full = os.getenv("YORIK_DB_URL")
    if full:
        return full
    host = os.getenv("YORIK_DB_HOST", "127.0.0.1")
    port = os.getenv("YORIK_DB_PORT", "5435")
    user = os.getenv("YORIK_DB_USER", "postgres")
    pwd  = os.getenv("YORIK_DB_PASSWORD") or _read_supabase_postgres_password()
    name = name_override or os.getenv("YORIK_DB_NAME", "postgres")
    # urlencode the password — base64 randoms from Supabase's
    # generate-keys.sh can contain '+' / '/' / '=' which would break
    # the URL otherwise.
    from urllib.parse import quote_plus
    return f"postgresql://{user}:{quote_plus(pwd)}@{host}:{port}/{name}"


_pool_main: Optional[ConnectionPool] = None
_pool_docs: Optional[ConnectionPool] = None


def _configure_uuid_as_text(conn: psycopg.Connection) -> None:
    """Phase E — make every UUID column come back as a Python str
    instead of a uuid.UUID instance. Yorik's Pydantic models expect
    `str` for user_id / owner_user_id / etc.; without this, every
    UUID column triggers a `validation error: not a string` 500.

    Wires the cast at the type-loader layer once per pooled
    connection. The reverse direction (writing a Python str into a
    UUID column) already works out of the box — Postgres accepts
    the canonical UUID string form on INSERT/UPDATE."""
    from psycopg.types.string import TextLoader
    # UUID OID = 2950 in standard pg_catalog.
    conn.adapters.register_loader(2950, TextLoader)


def _ensure_pool(schema: Schema) -> ConnectionPool:
    """Open the pool for either the `main` schema (Yorik's family-DB
    tables in `public`) or the `docs` schema (paperless / wa / document
    chunks).

    `search_path` is set via the Postgres connection-level `options`
    parameter rather than a SET-statement on each new conn. psycopg_pool
    runs a rollback-style `reset` between checkouts that doesn't
    survive session-level SETs reliably across the lib's connection
    cycling — but `options=-c search_path=...` is enforced at libpq
    connect time and persists for the life of the connection."""
    global _pool_main, _pool_docs
    if schema == "main":
        if _pool_main is None:
            _pool_main = ConnectionPool(
                _build_url(),
                min_size=2, max_size=30,
                kwargs={
                    "row_factory": _row_factory(),
                    "autocommit": False,
                    "options": "-c search_path=public,docs",
                },
                configure=_configure_uuid_as_text,
                open=False,
            )
            _pool_main.open()
        return _pool_main
    if _pool_docs is None:
        # In single-database Yorik installs, both schemas (public + docs)
        # live in the same Postgres DB. The default is therefore to
        # reuse `YORIK_DB_NAME` here — only override via
        # YORIK_DOCS_DB_NAME if you've split them onto separate DBs.
        docs_db = (
            os.getenv("YORIK_DOCS_DB_NAME")
            or os.getenv("YORIK_DB_NAME")
            or "postgres"
        )
        _pool_docs = ConnectionPool(
            _build_url(name_override=docs_db),
            min_size=1, max_size=10,
            kwargs={
                "row_factory": _row_factory(),
                "autocommit": False,
                "options": "-c search_path=docs,public",
            },
            configure=_configure_uuid_as_text,
            open=False,
        )
        _pool_docs.open()
    return _pool_docs


@contextmanager
def conn_ctx_pg(schema: Schema = "main") -> Iterator[psycopg.Connection]:
    """Yield a psycopg connection from the appropriate pool. Commits on
    clean exit, rolls back on exception — mirrors sqlite3's
    `conn_ctx()` so call sites swap with no shape change."""
    pool = _ensure_pool(schema)
    with pool.connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def close_all_pools() -> None:
    """Called at shutdown so connections aren't leaked when the
    process exits cleanly. Safe to call multiple times."""
    global _pool_main, _pool_docs
    for p in (_pool_main, _pool_docs):
        if p is not None:
            try:
                p.close()
            except Exception:  # noqa: BLE001
                pass
    _pool_main = None
    _pool_docs = None
