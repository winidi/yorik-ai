"""Backfill first_name / last_name on existing person contacts.

Migration 045 added the columns; this populates them from display_name
for every kind='person' row where first_name is still NULL. The split
heuristic is intentionally simple — first whitespace-separated token
becomes first_name, the rest joins as last_name. Imperfect for
compound surnames ("Maria von Habsburg-Lothringen") but the user can
fix per-row in the editor, and the imperfect-but-present split is
strictly better than the previous "no first/last at all" state.

Skipped (defensive):
  - kind='business'                       (these don't get a person name)
  - first_name already set                (don't clobber a manual edit)
  - display_name empty / single non-alnum (no signal to split)
"""
from __future__ import annotations


def up(conn) -> None:
    cur = conn.execute(
        "SELECT id, display_name FROM contacts "
        "WHERE kind = 'person' "
        "  AND (first_name IS NULL OR TRIM(first_name) = '') "
        "  AND display_name IS NOT NULL "
        "  AND TRIM(display_name) != ''"
    )
    updates: list[tuple[str, str | None, int]] = []
    for row in cur.fetchall():
        raw = (row["display_name"] or "").strip()
        if not raw:
            continue
        parts = raw.split()
        if not parts:
            continue
        first = parts[0]
        rest = " ".join(parts[1:]).strip() or None
        updates.append((first, rest, int(row["id"])))

    if not updates:
        return

    conn.executemany(
        "UPDATE contacts SET first_name = ?, last_name = ? WHERE id = ?",
        updates,
    )
