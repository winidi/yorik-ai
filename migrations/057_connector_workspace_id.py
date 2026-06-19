"""Phase C — add workspace_id to connector_credentials + connector_grants.

These tables hold encrypted OAuth tokens, IMAP passwords, per-user
Paperless / Immich API keys, and connector grants. The current
isolation is by-naming-convention (e.g. `paperless_user_1` belongs to
Yorik user 1) — which works because user_ids are globally unique, but
adds zero defence at the SQL layer.

This migration adds `workspace_id` so future multi-family code can
ALSO filter at the query level — e.g. when Workspace B's code paths
look up by user_id, a stray "give me user_1's token" can be guarded
by an `AND workspace_id = ?` clause. Today (single-workspace), every
row backfills to workspace 1.

Note `connector_credentials` uses `connector_name` as its TEXT primary
key, not an integer id — so we don't add an auto-id, just the new
scoping column. Read paths continue to work via the existing
name-based lookup; the column is purely additive defence-in-depth.

Phase C step T4.a. No read-path change in this step.
"""
from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    for table in ("connector_credentials", "connector_grants"):
        cols = {c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "workspace_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id INTEGER")

        # Backfill: every existing row in single-family installs belongs to
        # the seed workspace (id=1, created in migration 036).
        conn.execute(
            f"UPDATE {table} SET workspace_id = 1 WHERE workspace_id IS NULL"
        )

        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_workspace_id "
            f"ON {table} (workspace_id)"
        )
