"""Phase C — scope the remaining unscoped tables.

Coverage decisions (per the Phase C audit):

  notifications                 → space_id   (per-user notifications)
  saved_queries                 → workspace_id (voice trigger phrases
                                  belong to a household, not globally)
  document_series               → workspace_id (invoice numbering schemes
                                  per business / household)
  document_series_allocations   → workspace_id (inherit from series)

Explicitly deferred:
  app_settings — truly global config (HTTP toggles, voice prefs, etc.).
    The audit recommends splitting into global_settings + workspace_-
    settings; that's invasive and the current rows are all platform-
    level toggles. Revisit when multi-family runs with users editing
    Settings concurrently.

Phase C step T8.
"""
from __future__ import annotations

import sqlite3


def _add_col(conn: sqlite3.Connection, table: str, col: str, type_: str) -> None:
    cols = {c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_}")


def up(conn: sqlite3.Connection) -> None:
    # 1. notifications — per-user → personal space
    _add_col(conn, "notifications", "space_id", "INTEGER")
    conn.execute(
        "UPDATE notifications "
        "SET space_id = ("
        "  SELECT s.id FROM spaces s "
        "  WHERE s.kind='personal' AND s.owner_user_id = notifications.user_id "
        "  LIMIT 1"
        ") "
        "WHERE space_id IS NULL AND user_id IS NOT NULL"
    )
    conn.execute(
        "UPDATE notifications SET space_id = 1 WHERE space_id IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_space_id "
        "ON notifications (space_id)"
    )

    # 2. saved_queries — per-workspace voice trigger phrases
    _add_col(conn, "saved_queries", "workspace_id", "INTEGER")
    conn.execute(
        "UPDATE saved_queries SET workspace_id = 1 WHERE workspace_id IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_saved_queries_workspace_id "
        "ON saved_queries (workspace_id)"
    )

    # 3. document_series — per-workspace invoice numbering (currently empty)
    _add_col(conn, "document_series", "workspace_id", "INTEGER")
    conn.execute(
        "UPDATE document_series SET workspace_id = 1 WHERE workspace_id IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_series_workspace_id "
        "ON document_series (workspace_id)"
    )

    # 4. document_series_allocations — inherit from series (also empty)
    _add_col(conn, "document_series_allocations", "workspace_id", "INTEGER")
    conn.execute(
        "UPDATE document_series_allocations "
        "SET workspace_id = ("
        "  SELECT s.workspace_id FROM document_series s "
        "  WHERE s.id = document_series_allocations.series_id"
        ") "
        "WHERE workspace_id IS NULL"
    )
    conn.execute(
        "UPDATE document_series_allocations SET workspace_id = 1 "
        "WHERE workspace_id IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_series_allocations_workspace_id "
        "ON document_series_allocations (workspace_id)"
    )
