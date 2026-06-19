"""Phase C — add space_id to paperless_chunks in documents.db.

Purely additive migration:
  - Adds `space_id INTEGER` column to documents.db `paperless_chunks`
  - Backfills NULL rows to space_id=1 (the "Shared" household space)
  - Indexes the column for the upcoming row_filter() queries

Idempotent: safe to run on a DB that already has the column / data.
No read-path change yet — the reads in `paperless_ingest.py` ignore
the new column until a follow-up migration enables filtering. So a
fresh apply produces zero behavioural change; this is intentional
(makes regression bisection trivial if anything breaks later).

Phase C step T2.a. The read-path filtering ships as a separate
migration (T2.b) so each step is independently verifiable against
the smoke harness.
"""
from __future__ import annotations

import sqlite3
from typing import Any

# This migration touches documents.db rather than family.db. The
# migration runner's `conn` argument is the family.db connection;
# we open documents.db ourselves via the same path-resolver Yorik
# uses everywhere else.


def up(conn: sqlite3.Connection) -> None:
    """Add space_id to paperless_chunks in documents.db."""
    from backend.database import DEFAULT_DOCS_DB_PATH

    docs = sqlite3.connect(DEFAULT_DOCS_DB_PATH)
    try:
        # 0. Skip the whole thing if paperless_chunks doesn't exist yet.
        # On a virgin install (cold-install smoke, fresh Hetzner, etc.)
        # the documents.db table is created lazily by the first
        # paperless ingest; running this migration before then was
        # crashing with "no such table". A later ingest will create
        # the table WITH the space_id column from the start, so
        # skipping here is correct — the migration only matters for
        # boxes that ALREADY ingested before this column shipped.
        table_exists = docs.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paperless_chunks'"
        ).fetchone()
        if not table_exists:
            return

        # 1. Add column if it doesn't exist (idempotent)
        cols = {c[1] for c in docs.execute("PRAGMA table_info(paperless_chunks)").fetchall()}
        if "space_id" not in cols:
            docs.execute("ALTER TABLE paperless_chunks ADD COLUMN space_id INTEGER")

        # 2. Backfill any NULL rows to the Shared household space (id=1).
        # This is the workspace-1 Shared space per the existing seed; any
        # legitimately fresh install will have it created by 036.
        docs.execute(
            "UPDATE paperless_chunks SET space_id = 1 WHERE space_id IS NULL"
        )

        # 3. Index for the future row_filter() queries (T2.b)
        docs.execute(
            "CREATE INDEX IF NOT EXISTS idx_paperless_chunks_space_id "
            "ON paperless_chunks (space_id)"
        )

        docs.commit()
    finally:
        docs.close()
