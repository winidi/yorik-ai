"""Phase C — add space_id to events.

Backfill rule: events inherit `space_id` from their `calendar_id`. The
calendars table already has space_id (migration 036). This gives us a
clean transitive mapping with no judgement calls:

    event.space_id ← calendar.space_id  (where calendar_id is set)

Orphan fallbacks for safety:
  - event has owner_user_id but no calendar_id → owner's personal space
  - event has neither → workspace's shared space (id=1)

Idempotent: ADD COLUMN guarded by PRAGMA check; UPDATE clauses re-running
on already-backfilled rows produce no change.

Phase C step T3.a. Read-path filtering ships separately as T3.b so each
step is independently verifiable against the smoke harness.
"""
from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    # 1. ADD COLUMN if missing (SQLite has no "IF NOT EXISTS" on ALTER)
    cols = {c[1] for c in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "space_id" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN space_id INTEGER")

    # 2. Primary backfill: inherit from calendar
    conn.execute(
        "UPDATE events "
        "SET space_id = (SELECT c.space_id FROM calendars c WHERE c.id = events.calendar_id) "
        "WHERE space_id IS NULL AND calendar_id IS NOT NULL"
    )

    # 3. Orphan with owner → owner's personal space
    conn.execute(
        "UPDATE events "
        "SET space_id = ("
        "  SELECT s.id FROM spaces s "
        "  WHERE s.kind='personal' AND s.owner_user_id = events.owner_user_id "
        "  LIMIT 1"
        ") "
        "WHERE space_id IS NULL AND owner_user_id IS NOT NULL"
    )

    # 4. Orphan with no owner — workspace shared space (id=1 by Phase B seed)
    conn.execute(
        "UPDATE events SET space_id = 1 WHERE space_id IS NULL"
    )

    # 5. Index for the upcoming row_filter() queries (T3.b)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_space_id ON events (space_id)"
    )
