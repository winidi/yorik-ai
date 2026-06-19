"""Phase C — add space_id to 4 WhatsApp tables.

All four tables (wa_chats, wa_messages, wa_drafts, wa_self_identity)
already carry owner_user_id, so backfill is the same pattern as the
email migration: owner's personal space, fallback to workspace shared.

Phase C step T7.
"""
from __future__ import annotations

import sqlite3

WA_TABLES = ("wa_chats", "wa_messages", "wa_drafts", "wa_self_identity")


def up(conn: sqlite3.Connection) -> None:
    for table in WA_TABLES:
        cols = {c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "space_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN space_id INTEGER")

        conn.execute(
            f"UPDATE {table} "
            f"SET space_id = ("
            f"  SELECT s.id FROM spaces s "
            f"  WHERE s.kind='personal' AND s.owner_user_id = {table}.owner_user_id "
            f"  LIMIT 1"
            f") "
            f"WHERE space_id IS NULL AND owner_user_id IS NOT NULL"
        )

        conn.execute(f"UPDATE {table} SET space_id = 1 WHERE space_id IS NULL")

        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_space_id "
            f"ON {table} (space_id)"
        )
