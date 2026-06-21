"""Per-user contact preferences.

Backs the per-(contact, user) opt-in flag the suggestion engine
checks before analysing any message. Single source of truth lives
in contact_user_prefs; this module is the thin accessor everything
else uses so the underlying table can evolve (more pref columns,
per-modality opt-ins) without touching call sites.

Defaults to OFF on missing row — privacy-safe: a user who has never
flipped a contact's toggle gets no analysis on that contact's mail.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

log = logging.getLogger("yorik.contact_user_prefs")


def is_assist_enabled(conn, contact_id: int, user_id: str) -> bool:
    """Has the user explicitly enabled AI assist for this contact?
    Returns False on missing row — that's the privacy default; no
    analysis happens without an explicit opt-in."""
    if contact_id is None or not user_id:
        return False
    row = conn.execute(
        "SELECT yorik_assist_enabled FROM contact_user_prefs "
        "WHERE contact_id=? AND user_id=?",
        (int(contact_id), user_id),
    ).fetchone()
    return bool(row and row["yorik_assist_enabled"])


def set_assist_enabled(conn, contact_id: int, user_id: str, enabled: bool) -> None:
    """Upsert the per-user flag. updated_at refreshes on every flip
    so we can answer "when did this user enable AI for this contact"
    for the audit trail."""
    conn.execute(
        "INSERT INTO contact_user_prefs (contact_id, user_id, yorik_assist_enabled) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (contact_id, user_id) DO UPDATE "
        "  SET yorik_assist_enabled = EXCLUDED.yorik_assist_enabled, "
        "      updated_at = NOW()",
        (int(contact_id), user_id, bool(enabled)),
    )


def bulk_set_assist_enabled(conn, contact_ids: Iterable[int],
                            user_id: str, enabled: bool) -> int:
    """Upsert many at once. Returns the count of contacts touched.
    Used by the "Enable AI · N" bulk button so flipping 200+
    contacts is one round-trip instead of N."""
    ids = [int(c) for c in contact_ids if c is not None]
    if not ids:
        return 0
    # Postgres-flavoured executemany via VALUES; psycopg's
    # mogrify-based db_shim handles the parameter binding.
    args_seq = [(cid, user_id, bool(enabled)) for cid in ids]
    conn.executemany(
        "INSERT INTO contact_user_prefs (contact_id, user_id, yorik_assist_enabled) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (contact_id, user_id) DO UPDATE "
        "  SET yorik_assist_enabled = EXCLUDED.yorik_assist_enabled, "
        "      updated_at = NOW()",
        args_seq,
    )
    return len(ids)


def enabled_map_for_user(conn, contact_ids: Iterable[int],
                         user_id: str) -> dict[int, bool]:
    """Bulk fetch of yorik_assist_enabled for a list of contacts +
    one user. Used by the contacts-list serializer so the page load
    doesn't fire N+1 pref lookups. Missing rows → False."""
    ids = [int(c) for c in contact_ids if c is not None]
    if not ids:
        return {}
    placeholders = ",".join(["?"] * len(ids))
    rows = conn.execute(
        f"SELECT contact_id, yorik_assist_enabled "
        f"FROM contact_user_prefs "
        f"WHERE user_id=? AND contact_id IN ({placeholders})",
        (user_id, *ids),
    ).fetchall()
    return {int(r["contact_id"]): bool(r["yorik_assist_enabled"]) for r in rows}
