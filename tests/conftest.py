"""Shared pytest fixtures.

Every test runs against a throwaway Postgres database in the same
cluster Yorik itself uses (the bundled Supabase `supabase-db`, or
whatever `YORIK_DB_HOST/PORT` point at). Postgres is the only backend
Yorik ships; testing it against SQLite proved nothing about the code
that actually runs.

How it works:

  * Once per session, `yorik_test_template` is (re)built exactly the way
    `scripts/create-tenant.sh` builds a fresh tenant database: auth shim,
    `migrations_pg/100+` applied as `supabase_admin`, runtime grants to
    the `postgres` role the app's pool uses.
  * Each test gets its own `CREATE DATABASE … TEMPLATE yorik_test_template`
    (a file-level copy, ~100 ms), the backend modules are re-imported so
    module-level constants bind to it, and the database is dropped at
    teardown.

Nothing here ever touches the `postgres` database that holds real data.

Requirements: the Supabase stack up (`bash start.sh` or
`bash scripts/bootstrap-supabase.sh`) and the admin password reachable
via `YORIK_DB_PASSWORD` or `infra/supabase/docker/.env`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_PG = ROOT / "migrations_pg"
TEMPLATE_DB = "yorik_test_template"
TEST_DB_PREFIX = "yorik_test_"

# Migrations that only make sense with the full Supabase services stack
# on top of the database (Realtime publication, service_role grants,
# PostgREST GUC). scripts/create-tenant.sh skips the same set and marks
# them applied so the app's startup runner doesn't retry them.
_CLUSTER_ONLY_VERSIONS = {104, 107, 108}
# 000 is the Phase D SQLite-import bridge; fresh databases start at 100.
_LEGACY_VERSIONS = {0}

_AUTH_SHIM_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (
  id UUID PRIMARY KEY,
  email TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
  LANGUAGE sql STABLE
  AS $$ SELECT NULL::uuid $$;
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);
"""

_GRANTS_SQL = """
GRANT ALL ON DATABASE "{db}" TO postgres;
GRANT USAGE, CREATE ON SCHEMA public, yorik, docs, auth TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA public TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA docs   TO postgres;
GRANT ALL ON ALL TABLES    IN SCHEMA auth   TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO postgres;
GRANT ALL ON ALL SEQUENCES IN SCHEMA docs   TO postgres;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA yorik  TO postgres;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA auth   TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA docs   GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth   GRANT ALL ON TABLES TO postgres;
"""


def _pg_settings() -> dict[str, str]:
    """Host/port/password for the admin connection. Importing `backend`
    loads config.env, so the same values the app uses apply here."""
    import backend  # noqa: F401  (loads config.env into os.environ)
    from backend.database_pg import _read_supabase_postgres_password

    pw = os.getenv("YORIK_DB_PASSWORD") or _read_supabase_postgres_password()
    return {
        "host": os.getenv("YORIK_DB_HOST", "127.0.0.1"),
        "port": os.getenv("YORIK_DB_PORT", "5435"),
        "password": pw,
    }


def _admin_conn(settings: dict[str, str], dbname: str = "postgres") -> psycopg.Connection:
    return psycopg.connect(
        host=settings["host"], port=settings["port"], dbname=dbname,
        user="supabase_admin", password=settings["password"],
        connect_timeout=5, autocommit=True,
    )


def _discover_migrations() -> list[tuple[int, str, Path]]:
    from backend.migrations import discover
    return [(m.version, m.name, m.path) for m in discover(MIGRATIONS_PG)]


def _build_template(settings: dict[str, str]) -> None:
    with _admin_conn(settings) as admin:
        # Sweep leftovers from crashed runs, then rebuild the template so
        # it always reflects the migrations in the working tree.
        rows = admin.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s",
            (TEST_DB_PREFIX + "%",),
        ).fetchall()
        for (name,) in rows:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{TEMPLATE_DB}"')

    with _admin_conn(settings, TEMPLATE_DB) as db:
        db.execute(_AUTH_SHIM_SQL)
        for version, name, path in _discover_migrations():
            if version in _LEGACY_VERSIONS or version in _CLUSTER_ONLY_VERSIONS:
                db.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) "
                    "ON CONFLICT (version) DO NOTHING", (version, name),
                )
                continue
            try:
                db.execute(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"migration {path.name} failed on the test template: {exc}") from exc
            db.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) "
                "ON CONFLICT (version) DO NOTHING", (version, name),
            )
        db.execute(_GRANTS_SQL.format(db=TEMPLATE_DB))


@pytest.fixture(scope="session")
def pg_template() -> dict[str, str]:
    settings = _pg_settings()
    try:
        with _admin_conn(settings) as admin:
            admin.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        pytest.exit(
            f"Postgres not reachable at {settings['host']}:{settings['port']} as supabase_admin "
            f"({type(exc).__name__}: {exc}). Tests need the bundled Supabase stack: "
            f"run `bash start.sh` or `bash scripts/bootstrap-supabase.sh` first.",
            returncode=3,
        )
    _build_template(settings)
    return settings


_counter = 0


def _close_pools_if_loaded() -> None:
    mod = sys.modules.get("backend.database_pg")
    if mod is not None:
        try:
            mod.close_all_pools()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def fresh_app(monkeypatch: pytest.MonkeyPatch, pg_template: dict[str, str]) -> Iterator:
    """Reload backend.main against an empty, fully migrated database.
    Yields the FastAPI app instance. Tests should use `TestClient(fresh_app)`
    for the request interface."""
    global _counter
    _counter += 1
    dbname = f"{TEST_DB_PREFIX}{os.getpid()}_{_counter}"

    _close_pools_if_loaded()
    with _admin_conn(pg_template) as admin:
        admin.execute(f'CREATE DATABASE "{dbname}" TEMPLATE "{TEMPLATE_DB}"')

    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setenv("YORIK_DB_BACKEND", "postgres")
    monkeypatch.setenv("YORIK_DB_HOST", pg_template["host"])
    monkeypatch.setenv("YORIK_DB_PORT", pg_template["port"])
    monkeypatch.setenv("YORIK_DB_USER", "postgres")
    monkeypatch.setenv("YORIK_DB_PASSWORD", pg_template["password"])
    monkeypatch.setenv("YORIK_DB_NAME", dbname)
    monkeypatch.delenv("YORIK_DOCS_DB_NAME", raising=False)
    monkeypatch.delenv("YORIK_DB_URL", raising=False)
    # Files Yorik writes next to the database stay in the tmp dir.
    monkeypatch.setenv("HOMEOS_DB_PATH", str(Path(tmp.name) / "family.db"))
    monkeypatch.setenv("HOMEOS_DOCS_DIR", str(Path(tmp.name) / "docs"))
    monkeypatch.setenv("HOMEOS_DOCS_DB_PATH", str(Path(tmp.name) / "documents.db"))

    # Force re-import so module-level constants bind to the env vars we
    # just set, even if a previous test already loaded the module.
    import importlib  # noqa: F401
    for mod_name in list(sys.modules):
        if mod_name.startswith("backend"):
            del sys.modules[mod_name]
    from backend import main as backend_main  # noqa: E402

    try:
        yield backend_main.app
    finally:
        _close_pools_if_loaded()
        with _admin_conn(pg_template) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        tmp.cleanup()


# ─── shared helpers ───────────────────────────────────────────────────
# user_profiles.id is a UUID referencing auth.users(id) since Phase E.
# Tests must never INSERT integer ids or rely on lastrowid for users;
# go through these instead.

def seed_user(*, name: str, role: str = "admin", email: str | None = None,
              password: str | None = None, voice_id: str | None = None,
              language: str = "en", **extra_columns) -> str:
    """Insert a user the way backend.users.create_user does (auth.users
    shim row + user_profiles row) and return its UUID as a string."""
    import uuid
    from backend.database import DEFAULT_DB_PATH, conn_ctx
    from backend import auth_sessions

    uid = str(uuid.uuid4())
    email = email or f"{name.lower().replace(' ', '.')}@example.local"
    cols = {"id": uid, "name": name, "email": email, "role": role,
            "voice_id": voice_id or f"vid-{uid[:8]}", "language": language}
    if password is not None:
        cols["password_hash"] = auth_sessions.hash_password(password)
    cols.update(extra_columns)
    placeholders = ", ".join("?" for _ in cols)
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        conn.execute("INSERT INTO auth.users (id, email) VALUES (?, ?) ON CONFLICT (id) DO NOTHING",
                     (uid, email))
        conn.execute(f"INSERT INTO user_profiles ({', '.join(cols)}) VALUES ({placeholders})",
                     tuple(cols.values()))
        conn.commit()
    return uid


def login_client(app, *, role: str = "admin", name: str | None = None,
                 email: str | None = None):
    """A TestClient carrying a real session cookie for a freshly seeded
    user of the given role. Returns (client, user_id)."""
    from fastapi.testclient import TestClient
    from backend import auth_sessions

    uid = seed_user(name=name or f"{role}-user", role=role,
                    email=email or f"{role}@example.local", password="pytestpw123")
    sid = auth_sessions.create_session(uid, user_agent="pytest", ip="127.0.0.1")
    client = TestClient(app)
    client.cookies.set(auth_sessions.COOKIE_NAME, sid)
    return client, uid
