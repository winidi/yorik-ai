"""037 — Phase B.2.1: drop the allowed_roles column from family.db.

The column is dead data after 036 + the Phase B code flip. All writes
have been removed (events / tasks / contacts / bills + the seed code +
the rollback INSERTs + the recurring-task materialiser); all reads now
go through spaces.* helpers instead of the role-allowlist clause.

Also drops the two legacy share tables (calendar_shares, contact_shares)
that are superseded by space_members and row_shares. Migration 036
preserved access for the one existing calendar_shares row via the
Household space membership; contact_shares had 0 rows.

Requires SQLite >= 3.35 (native DROP COLUMN). 3.45 ships with Python
3.12 — covers every supported install. Older Python falls back to the
table-rebuild dance; we don't bother since stdlib already meets the
floor.
"""
from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row

    for table in ("events", "tasks", "contacts", "bills"):
        if _column_exists(cur, table, "allowed_roles"):
            cur.execute(f"ALTER TABLE {table} DROP COLUMN allowed_roles")

    for legacy_table in ("calendar_shares", "contact_shares"):
        if _table_exists(cur, legacy_table):
            cur.execute(f"DROP TABLE {legacy_table}")


def _column_exists(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    row = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone()
    return row is not None
