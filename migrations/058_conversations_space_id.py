"""Phase C — add space_id to legacy `conversations` table.

The live chat history lives in `agent_conversations` (Phase B already
gave it `space_id`). The bare `conversations` table is the legacy
Vanna persistence layer, still referenced by backend/ask.py's
ui-action stash path. Production rows: 0 — but conversation_store.py
still has live INSERT OR REPLACE statements, so any future code path
that hits it would write workspace-unscoped rows.

This migration adds `space_id` as defence-in-depth. Read-path change
deferred (conversation_store usage is narrow and dead in current
flow; revisit if/when rows accumulate).

Phase C step T5.
"""
from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    cols = {c[1] for c in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if "space_id" not in cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN space_id INTEGER")

    # 0 rows to backfill in current state; statement is a no-op safety net
    # so subsequent runs of this migration on a populated table behave
    # sanely.
    conn.execute(
        "UPDATE conversations SET space_id = 1 WHERE space_id IS NULL"
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_space_id "
        "ON conversations (space_id)"
    )
