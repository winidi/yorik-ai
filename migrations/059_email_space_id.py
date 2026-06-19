"""Phase C — add space_id to 5 email tables.

Tables + backfill rule:
  email_accounts     — owner_user_id → owner's personal space
  email_messages     — owner_user_id → owner's personal space
  email_drafts       — owner_user_id → owner's personal space
  email_folders      — inherit from email_accounts via account_id FK
  email_attachments  — inherit from email_messages via message_id FK

Order matters: backfill the owner-keyed tables first, then the FK-
dependent ones. Any row that doesn't match its parent (orphan) falls
back to the workspace shared space (id=1).

Phase C step T6.
"""
from __future__ import annotations

import sqlite3

OWNER_KEYED = ("email_accounts", "email_messages", "email_drafts")
DERIVED = (
    ("email_folders", "account_id", "email_accounts"),
    ("email_attachments", "message_id", "email_messages"),
)


def up(conn: sqlite3.Connection) -> None:
    all_tables = OWNER_KEYED + tuple(t[0] for t in DERIVED)

    # 1. Add column to each (idempotent)
    for table in all_tables:
        cols = {c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "space_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN space_id INTEGER")

    # 2. Owner-keyed: pull from spaces by owner_user_id
    for table in OWNER_KEYED:
        conn.execute(
            f"UPDATE {table} "
            f"SET space_id = ("
            f"  SELECT s.id FROM spaces s "
            f"  WHERE s.kind='personal' AND s.owner_user_id = {table}.owner_user_id "
            f"  LIMIT 1"
            f") "
            f"WHERE space_id IS NULL AND owner_user_id IS NOT NULL"
        )

    # 3. Derived: inherit from FK parent
    for table, fk_col, parent in DERIVED:
        conn.execute(
            f"UPDATE {table} "
            f"SET space_id = ("
            f"  SELECT p.space_id FROM {parent} p WHERE p.id = {table}.{fk_col}"
            f") "
            f"WHERE space_id IS NULL"
        )

    # 4. Fallback: any remaining NULL → workspace shared space
    for table in all_tables:
        conn.execute(f"UPDATE {table} SET space_id = 1 WHERE space_id IS NULL")

    # 5. Indexes
    for table in all_tables:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_space_id "
            f"ON {table} (space_id)"
        )
