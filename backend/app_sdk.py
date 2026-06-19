# SPDX-License-Identifier: AGPL-3.0-or-later WITH Yorik-App-SDK-Exception-1.0
#
# This file is part of the Yorik App SDK and is covered by the linking
# exception in LICENSE-EXCEPTION-APP-SDK at the repository root. That
# exception lets third-party Yorik apps import from this module without
# inheriting the AGPL's copyleft on their own code — so commercial /
# proprietary / differently-licensed apps can coexist with open-source
# ones in the Yorik ecosystem.
#
# Modifications to THIS file remain AGPL-3.0-or-later; the exception
# applies to combination, not relicensing of the SDK itself.

"""App SDK — what every community-app's connector.py imports.

A community app's connector.py looks like:

    from yorik.app_sdk import operation, db, family, connector

    @operation(role=["admin", "employee"])
    def customer_add(name: str, email: str = "") -> dict:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO customers (name, email) VALUES (?, ?)",
                (name, email),
            )
            return {"id": cur.lastrowid, "name": name}

    @operation(role=["admin"])
    def quote_create(customer_id: int, total: float) -> dict:
        with db() as conn:
            cur = conn.execute(...)
            qid = cur.lastrowid
        # Cross-app write into family.db requires a grant declared in
        # manifest.json's requires_tables_external + accepted by the user.
        with family() as conn:
            conn.execute("INSERT INTO events (title, starts_at) VALUES (?, ?)",
                         (f"Follow up quote #{qid}", "2026-06-01T09:00:00"))
        return {"quote_id": qid}

When the app is loaded, app_loader.py walks its connector module and finds
functions tagged with @operation. Each becomes a connector spec registered
with backend="app:<id>". The LLM then sees them via list_connectors and
calls them via trigger_connector.
"""

from __future__ import annotations

import logging
import sqlite3
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("homeos.app_sdk")

# Per-call context: which app is currently running an operation?
# Set by the wrapper in app_loader.register_app_operations before calling
# the user's function; reset on exit. Used by db() / family() / connector()
# to know who's asking.
_active_app: ContextVar[Optional[str]] = ContextVar("_active_app", default=None)


def _current_app() -> str:
    """Return the app id whose operation is currently executing. Raises if
    called outside an @operation context (e.g. from app's top-level code)."""
    aid = _active_app.get()
    if not aid:
        raise RuntimeError(
            "app_sdk.db()/family()/etc. can only be called inside an @operation function. "
            "Module-level use isn't supported (apps don't get an ambient identity at import time)."
        )
    return aid


# ─── @operation decorator ──────────────────────────────────────────────────

def operation(
    *,
    role: Optional[List[str]] = None,
    description: Optional[str] = None,
    name: Optional[str] = None,
) -> Callable:
    """Mark a function as an LLM-callable operation on this app.

    Args:
      role: who may invoke it. Default ["admin"]. Children/viewers etc. must
            be explicitly listed.
      description: shown to the LLM in list_connectors. Defaults to the
                   function's docstring.
      name: operation name (defaults to function name). Must be a valid
            Python identifier; the LLM uses {app_id}.{name} via trigger_connector.
    """
    allowed_roles = list(role) if role else ["admin"]

    def wrap(fn: Callable) -> Callable:
        fn._yorik_operation = True   # discovered by app_loader
        fn._yorik_op_role = allowed_roles
        fn._yorik_op_name = name or fn.__name__
        fn._yorik_op_doc = description or (fn.__doc__ or "").strip().split("\n")[0] or fn.__name__
        return fn

    return wrap


# ─── DB helpers (called inside operation functions) ────────────────────────

def db(*, app_id: Optional[str] = None) -> sqlite3.Connection:
    """Open a connection to the calling app's OWN data.db.

    Path: data/apps/<app_id>/data.db. Auto-creates the directory + file the
    first time it's needed. The schema must already have been applied (the
    app loader does that at install time).
    """
    aid = app_id or _current_app()
    from .database import DEFAULT_DB_PATH  # avoid import cycle
    base = Path(DEFAULT_DB_PATH).resolve().parent / "apps" / aid
    base.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(base / "data.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def pg_db(*, app_id: Optional[str] = None):
    """Phase E v2 SDK: yield a psycopg connection scoped to the app's
    owned Postgres schema.

    `search_path` is set to `app_<id>, public` so `SELECT * FROM notes`
    inside the operation hits the app's own table. The caller writes
    the same SQL they declared in schema.sql.

    Used as a context manager:
        with pg_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO notes (body) VALUES (%s)", (body,))

    Yorik FastAPI's pool runs as the postgres superuser (BYPASSRLS),
    so the connector can read/write the app's data regardless of the
    end-user's JWT. RLS still applies when the app's iframe talks to
    Supabase directly with a user JWT.
    """
    aid = app_id or _current_app()
    from . import app_schema_lifecycle, database_pg
    from contextlib import contextmanager

    installed = app_schema_lifecycle.get_installed_app(aid)
    schema = (
        installed["owned_schema"]
        if installed
        else app_schema_lifecycle.owned_schema_for({"id": aid})
    )

    @contextmanager
    def _scoped():
        with database_pg.conn_ctx_pg("main") as conn:
            with conn.cursor() as cur:
                cur.execute(f'SET LOCAL search_path = "{schema}", public')
            yield conn

    return _scoped()


def _check_grant(app_id: str, resource_type: str, resource_db: Optional[str],
                resource_name: str, want_access: str) -> bool:
    """Look up app_grants in family.db. Returns True iff a non-revoked grant
    exists that covers the requested access level."""
    from .database import conn_ctx, DEFAULT_DB_PATH
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        row = conn.execute(
            "SELECT access FROM app_grants "
            "WHERE app_id = ? AND resource_type = ? AND "
            "      (resource_db = ? OR (resource_db IS NULL AND ? IS NULL)) AND "
            "      resource_name = ? AND revoked_at IS NULL",
            (app_id, resource_type, resource_db, resource_db, resource_name),
        ).fetchone()
    if not row:
        return False
    granted = row["access"]  # 'read' | 'write' | 'read+write'
    if want_access == "read":
        return granted in ("read", "read+write")
    if want_access == "write":
        return granted in ("write", "read+write")
    return False


class GrantError(PermissionError):
    """Raised when an app tries to read/write a resource it lacks a grant for."""


def family(*, app_id: Optional[str] = None, write: bool = False) -> sqlite3.Connection:
    """Open a connection to family.db.

    The CALLING app must have a `table` grant for whatever it's about to query.
    We don't parse the SQL — we trust the app declared its tables in
    manifest.json:requires_tables_external and that CI verified the code
    matches. The grant system is the user-consent layer on top of that.

    A read-only grant blocks writes via PRAGMA query_only on the connection.
    """
    aid = app_id or _current_app()
    # Coarse check: app must have AT LEAST ONE grant against family. The
    # operation author is responsible for not over-reaching.
    from .database import conn_ctx, DEFAULT_DB_PATH
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT resource_name, access FROM app_grants "
            "WHERE app_id = ? AND resource_type = 'table' AND resource_db = 'family' AND revoked_at IS NULL",
            (aid,),
        ).fetchall()
    if not rows:
        raise GrantError(
            f"app '{aid}' attempted to access family.db without a grant. "
            f"Declare requires_tables_external in manifest.json and prompt the user for consent."
        )
    if write and not any(r["access"] in ("write", "read+write") for r in rows):
        raise GrantError(
            f"app '{aid}' has read-only grants on family.db; write attempted."
        )
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if not write:
        conn.execute("PRAGMA query_only = 1;")
    return conn


def documents(*, app_id: Optional[str] = None) -> sqlite3.Connection:
    """Open a connection to documents.db. Requires a 'documents' table grant."""
    aid = app_id or _current_app()
    if not _check_grant(aid, "table", "documents", "documents", "read"):
        raise GrantError(
            f"app '{aid}' attempted to access documents.db without a grant."
        )
    from .database import get_docs_conn, DEFAULT_DOCS_DB_PATH
    return get_docs_conn(DEFAULT_DOCS_DB_PATH)


async def connector(name: str, params: Optional[Dict[str, Any]] = None,
                   *, app_id: Optional[str] = None) -> Dict[str, Any]:
    """Call another connector by name. Requires a connector grant."""
    aid = app_id or _current_app()
    if not _check_grant(aid, "connector", None, name, "read"):
        raise GrantError(
            f"app '{aid}' attempted to call connector '{name}' without a grant. "
            f"Declare requires_connectors in manifest.json."
        )
    from . import connectors as connectors_mod
    return await connectors_mod.invoke(name, params or {})


# ─── LLM access ───────────────────────────────────────────────────────────

class _LlmHandle:
    """Singleton handle exposed to apps as ``llm``.

    Use ``llm.ask(prompt, system=...)`` for one-shot completions or
    ``llm.complete(messages)`` for multi-turn. Apps don't pick the
    backend — they use whatever model + base_url the user configured
    for Yorik's own chat (Settings -> LLM).

    No tool-use surface: apps cannot define tools mid-completion and
    let the model orchestrate them. If you need that, build your own
    loop. The ``@operation`` functions YOU define are already exposed
    to Yorik's main agent via ``trigger_connector``; that's where the
    LLM-driven orchestration lives.

    Must be called from inside an ``@operation`` function — module-
    level use raises (apps don't have an ambient identity at import).
    Logs the calling app id so usage is auditable. A ``requires_llm``
    manifest field + grant gating is on the roadmap.
    """

    def ask(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """One-shot completion. Returns the assistant's reply text."""
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.complete(messages, max_tokens=max_tokens, temperature=temperature)

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Multi-message completion. ``messages`` is OpenAI-shape:
        ``[{"role": "system"|"user"|"assistant", "content": str}, ...]``.

        Returns the assistant's reply text. No tool-call orchestration.
        """
        aid = _current_app()
        from . import ask as _ask
        from .agent.llm import LlmClient
        client = LlmClient(
            model=_ask.LLM_MODEL,
            base_url=_ask.LLM_BASE_URL,
            api_key=_ask._boot_api_key(),
        )
        log.info("app %r called llm.complete (%d messages)", aid, len(messages))
        resp = client.chat(
            list(messages),
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.get("content") or ""


llm = _LlmHandle()


# ─── ContextVar plumbing for app_loader to use ────────────────────────────

def set_active_app(app_id: Optional[str]):
    """Set/clear the active app id. Used by the wrapper that app_loader
    installs around each registered operation."""
    return _active_app.set(app_id)


def reset_active_app(token):
    _active_app.reset(token)
