"""Phase B ACL primitives: workspaces / spaces / space_members / row_shares.

Migration 036 lays the schema; this module owns the read + write helpers.
Endpoints and skills consult these functions instead of the older
allowed_roles / calendar_shares / contact_shares mechanisms (those tables
still exist for now but their bespoke clauses are no longer queried —
see B.2 for column drops).

Mental model
------------
- A workspace is the top-level container (one per install today).
- A space is an ACL container within a workspace. Spaces are either
  personal (one per user, owner = that user) or shared (any number,
  members joined explicitly).
- Every domain row (event/task/contact/bill/conversation/...) lives in
  exactly one space (`<table>.space_id`). The row's `owner_user_id`
  determines who can delete it; the space determines who can see it.
- Per-row exceptions live in row_shares (level read or write).

A user can SEE a row when any of these hold:
  1. User is platform_admin (system-wide infra bypass).
  2. User owns the row.
  3. User is a member of the row's space (at any level).
  4. User has a row_shares entry for this row at level read or write.

A user can WRITE a row when any of these hold:
  1. Platform_admin.
  2. Owner.
  3. Space member at level write or admin.
  4. row_shares entry at level write.

Workspace admins (`role = 'admin'`) are NOT a bypass — they go through
the same membership pathway, scoped to spaces inside workspaces they
own (see user_visible_space_ids + user_space_level).

This module returns booleans + SQL fragments; it does not raise. Callers
wrap rejections in HTTP exceptions or skill errors as appropriate.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Literal, Optional

from .database import conn_ctx

Level = Literal["read", "write", "admin"]
_LEVEL_RANK = {"read": 1, "write": 2, "admin": 3}


# ─── Lookups ────────────────────────────────────────────────────────


def personal_space_id(user_id: str) -> Optional[int]:
    """Return the user's personal-space id (None if user has none yet —
    new-account provisioning hasn't created it). Cached on the hot path
    is possible but unmeasured; SQLite is fast on a single-row index."""
    with conn_ctx() as c:
        row = c.execute(
            "SELECT id FROM spaces WHERE kind='personal' AND owner_user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["id"]) if row else None


def user_visible_space_ids(user_id: Optional[int], role: Optional[str]) -> list[int]:
    """Spaces the user can see.

    Phase C role model:
      platform_admin → every space in every workspace (the infra
        operator). Preserves the single-family-admin "sees all" behaviour.
      admin → workspace admin: every space inside the workspaces this
        user owns (workspaces.owner_user_id = user.id), PLUS their
        personal space + shared-space memberships. Crucially this does
        NOT span across workspaces the user doesn't own.
      everyone else → personal space + explicit space_members rows only.

    Returns an empty list for None user (anonymous)."""
    if user_id is None:
        return []
    r = (role or "").lower()
    with conn_ctx() as c:
        if r == "platform_admin":
            rows = c.execute("SELECT id FROM spaces").fetchall()
            return [int(r["id"]) for r in rows]
        if r == "admin":
            # Owned workspaces' spaces ∪ personal ∪ memberships
            rows = c.execute(
                "SELECT id FROM spaces "
                "WHERE workspace_id IN "
                "  (SELECT id FROM workspaces WHERE owner_user_id = ?) "
                "UNION "
                "SELECT id FROM spaces WHERE owner_user_id = ? "
                "UNION "
                "SELECT space_id AS id FROM space_members WHERE user_id = ?",
                (user_id, user_id, user_id),
            ).fetchall()
            return [int(r["id"]) for r in rows]
        # member / restricted / child / employee / viewer
        rows = c.execute(
            "SELECT id FROM spaces WHERE owner_user_id = ? "
            "UNION "
            "SELECT space_id AS id FROM space_members WHERE user_id = ?",
            (user_id, user_id),
        ).fetchall()
        return [int(r["id"]) for r in rows]


def user_space_level(
    user_id: str, space_id: int, role: Optional[str] = None,
) -> Optional[Level]:
    """Returns the effective write level of a user in a given space, or
    None if they aren't a member and don't own it. Admin gets 'admin'
    everywhere; personal-space owner gets 'admin' on their own."""
    r = (role or "").lower()
    if r == "platform_admin":
        return "admin"
    with conn_ctx() as c:
        if r == "admin":
            # Workspace admin: admin on every space in workspaces they own
            owns = c.execute(
                "SELECT 1 FROM spaces "
                "WHERE id = ? AND workspace_id IN "
                "  (SELECT id FROM workspaces WHERE owner_user_id = ?)",
                (int(space_id), user_id),
            ).fetchone()
            if owns:
                return "admin"
            # Fall through — they may be a member of this space too.
        row = c.execute(
            "SELECT owner_user_id FROM spaces WHERE id = ?", (int(space_id),)
        ).fetchone()
        if row and row["owner_user_id"] is not None and int(row["owner_user_id"]) == user_id:
            return "admin"
        row = c.execute(
            "SELECT level FROM space_members WHERE space_id = ? AND user_id = ?",
            (int(space_id), user_id),
        ).fetchone()
        return row["level"] if row else None  # type: ignore[return-value]


def has_level(actual: Optional[Level], need: Level) -> bool:
    """True if `actual` is at least `need`."""
    if actual is None:
        return False
    return _LEVEL_RANK.get(actual, 0) >= _LEVEL_RANK.get(need, 0)


# ─── Per-row visibility ─────────────────────────────────────────────


def _resolve_row_space_id(table: str, row: dict[str, Any]) -> Optional[int]:
    """Return the row's space_id, resolving via parent when the table
    doesn't carry it directly. Events inherit from their calendar."""
    sid = row.get("space_id")
    if sid is not None:
        return int(sid)
    if table == "events":
        cal_id = row.get("calendar_id")
        if cal_id is None:
            return None
        with conn_ctx() as c:
            r = c.execute(
                "SELECT space_id FROM calendars WHERE id=?", (int(cal_id),)
            ).fetchone()
            if r and r["space_id"] is not None:
                return int(r["space_id"])
    return None


def can_view_row(
    user_id: Optional[int], role: Optional[str], table: str, row: dict[str, Any],
) -> bool:
    """Visibility check for a single hydrated row. Used by GET-by-id
    handlers. Lists use the SQL fragment from `row_filter()` instead."""
    if (role or "").lower() == "platform_admin":
        return True
    if user_id is None:
        return False
    owner = row.get("owner_user_id") or row.get("created_by_user_id")
    # Phase E: user ids are UUID strings — compare as strings.
    if owner is not None and str(owner) == str(user_id):
        return True
    space_id = _resolve_row_space_id(table, row)
    if space_id is not None and space_id in user_visible_space_ids(user_id, role):
        return True
    # Per-row share
    row_id = row.get("id")
    if row_id is not None:
        with conn_ctx() as c:
            share = c.execute(
                "SELECT 1 FROM row_shares "
                "WHERE table_name = ? AND row_id = ? AND user_id = ?",
                (table, int(row_id), user_id),
            ).fetchone()
            if share:
                return True
    return False


def can_write_row(
    user_id: Optional[int], role: Optional[str], table: str, row: dict[str, Any],
) -> bool:
    """Mutation check for a single row. Owner / platform_admin always;
    write member of the row's space (or, for events, the calendar's
    space); write-level row_shares entry. Workspace admins write via
    user_space_level('admin') for spaces in workspaces they own."""
    if (role or "").lower() == "platform_admin":
        return True
    if user_id is None:
        return False
    owner = row.get("owner_user_id") or row.get("created_by_user_id")
    # Phase E: user ids are UUID strings — compare as strings.
    if owner is not None and str(owner) == str(user_id):
        return True
    space_id = _resolve_row_space_id(table, row)
    if space_id is not None:
        level = user_space_level(user_id, space_id, role)
        if has_level(level, "write"):
            return True
    row_id = row.get("id")
    if row_id is not None:
        with conn_ctx() as c:
            share = c.execute(
                "SELECT level FROM row_shares "
                "WHERE table_name = ? AND row_id = ? AND user_id = ?",
                (table, int(row_id), user_id),
            ).fetchone()
            if share and (share["level"] or "") in ("write", "admin"):
                return True
    return False


# ─── SQL helpers for list endpoints ─────────────────────────────────


def row_filter(
    user_id: Optional[int], role: Optional[str], table: str,
    *, table_alias: Optional[str] = None,
) -> tuple[str, list[Any]]:
    """Return (sql_fragment, params) to AND into a list query. Filters
    `<table>` rows to ones the user can SEE per the spaces+shares model.

    platform_admin → empty fragment (true global bypass).
    admin → workspace-scoped: matches when space_id ∈ spaces in
            workspaces the user owns ∪ personal ∪ memberships.
    Member → matches when space_id ∈ visible spaces, OR row owner is
             the user, OR a row_shares row exists.

    `table_alias` lets the caller use it inside JOINs or with an alias.
    Defaults to the table name.
    """
    if (role or "").lower() == "platform_admin":
        return "1=1", []
    if user_id is None:
        return "1=0", []
    t = table_alias or table
    spaces = user_visible_space_ids(user_id, role)
    parts: list[str] = []
    params: list[Any] = []

    # Owner column varies per table. Tables without an owner column at
    # all (e.g. bills today) skip the owner clause — visibility is
    # purely space-membership + row_shares for those.
    owner_col = {
        "tasks":    "created_by_user_id",
        "contacts": "created_by_user_id",
        "events":   "owner_user_id",
        "calendars": "owner_user_id",
    }.get(table)
    if owner_col:
        parts.append(f"{t}.{owner_col} = ?")
        params.append(user_id)

    if spaces:
        placeholders = ",".join("?" * len(spaces))
        parts.append(f"{t}.space_id IN ({placeholders})")
        params.extend(spaces)

    parts.append(
        f"{t}.id IN (SELECT row_id FROM row_shares "
        f"           WHERE table_name = ? AND user_id = ?)"
    )
    params.extend([table, user_id])

    return "(" + " OR ".join(parts) + ")", params


# ─── Phase B onboarding helpers ─────────────────────────────────────


def ensure_workspace_exists(owner_user_id: str, owner_name: str) -> int:
    """Idempotent — creates the single per-install workspace if none
    yet, with the given user as owner. Returns the workspace id.

    Called by both `auth.auth_setup` (first-run admin) and
    `users.create_user` (admin creating other users post-setup). Default
    kind is 'family'; admin can switch via PATCH /api/workspaces/current.
    """
    with conn_ctx() as c:
        row = c.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
        if row:
            return int(row["id"])
        cur = c.execute(
            "INSERT INTO workspaces (name, kind, owner_user_id) "
            "VALUES (?, 'family', ?)",
            (f"{owner_name}'s household", owner_user_id),
        )
        ws_id = int(cur.lastrowid)
        # Seed the two reserved shared spaces. owner_user_id stays NULL
        # for shared spaces — the workspace owner has implicit admin
        # via being the workspace owner, plus we add them to both as
        # explicit members below.
        cur = c.execute(
            "INSERT INTO spaces (workspace_id, name, kind, slug) "
            "VALUES (?, 'Shared', 'shared', 'household')",
            (ws_id,),
        )
        household_id = int(cur.lastrowid)
        cur = c.execute(
            "INSERT INTO spaces (workspace_id, name, kind, slug) "
            "VALUES (?, 'Finance', 'shared', 'finance')",
            (ws_id,),
        )
        finance_id = int(cur.lastrowid)
        # Workspace owner is admin of both reserved shared spaces.
        for sid in (household_id, finance_id):
            c.execute(
                "INSERT INTO space_members (space_id, user_id, level) "
                "VALUES (?, ?, 'admin')",
                (sid, owner_user_id),
            )
        c.commit()
        return ws_id


def ensure_personal_space(user_id: str, name: str) -> Optional[int]:
    """Idempotent — creates the user's personal space if missing.
    Returns the space id, or None when no workspace exists yet (caller
    should have run `ensure_workspace_exists` first)."""
    with conn_ctx() as c:
        row = c.execute(
            "SELECT id FROM spaces WHERE kind='personal' AND owner_user_id=?",
            (user_id,),
        ).fetchone()
        if row:
            return int(row["id"])
        ws = c.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
        if not ws:
            return None
        cur = c.execute(
            "INSERT INTO spaces (workspace_id, name, kind, owner_user_id) "
            "VALUES (?, ?, 'personal', ?)",
            (ws["id"], f"{name}'s space", user_id),
        )
        c.commit()
        return int(cur.lastrowid)


def add_user_to_household(user_id: str, level: Level = "write") -> Optional[int]:
    """Idempotent — add the user to the Household shared space + fire
    the provisioning hook so Paperless / Immich pick them up. Returns
    household space id on success, None when no Household exists."""
    with conn_ctx() as c:
        row = c.execute(
            "SELECT id FROM spaces WHERE slug='household' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        household_id = int(row["id"])
        c.execute(
            "INSERT OR IGNORE INTO space_members (space_id, user_id, level) "
            "VALUES (?, ?, ?)",
            (household_id, user_id, level),
        )
        c.commit()
    on_space_member_added(household_id, user_id, level)
    return household_id


def backfill_calendar_space_ids(user_id: str) -> None:
    """Set space_id on calendars that came up with NULL — happens when
    a calendar is created BEFORE the user's personal space exists (the
    fresh-install order in auth.auth_setup). Idempotent.

    Personal calendars → owner's personal space.
    Shared calendars (the single 'Shared' one) → Household.
    """
    with conn_ctx() as c:
        personal = c.execute(
            "SELECT id FROM spaces WHERE kind='personal' AND owner_user_id=?",
            (user_id,),
        ).fetchone()
        household = c.execute(
            "SELECT id FROM spaces WHERE slug='household' LIMIT 1"
        ).fetchone()
        if personal:
            c.execute(
                "UPDATE calendars SET space_id=? "
                "WHERE owner_user_id=? AND kind='personal' AND space_id IS NULL",
                (personal["id"], user_id),
            )
        if household:
            c.execute(
                "UPDATE calendars SET space_id=? "
                "WHERE kind='shared' AND space_id IS NULL",
                (household["id"],),
            )
        c.commit()


# ─── Provisioning hooks (placeholders for B.3 / B.4) ────────────────
# When users join/leave a space, the Paperless+Immich provisioning hooks
# fire here. Empty no-ops today; B.3+ wires real HTTP calls.


def on_space_member_added(space_id: int, user_id: str, level: Level) -> None:
    """Called after a space_members INSERT. Triggers Paperless +
    Immich provisioning sync so the user's external accounts gain
    access to the space's resources. Best-effort: provisioning failures
    are logged but don't block the membership change."""
    _provision_external("added", space_id, user_id)


def on_space_member_removed(space_id: int, user_id: str) -> None:
    """Called after a space_members DELETE. Revokes Paperless + Immich
    access by re-syncing the space (Yorik membership is the source of
    truth; each connector diffs against its own state and removes
    anyone no longer a member)."""
    _provision_external("removed", space_id, user_id)


def _provision_external(action: str, space_id: int, user_id: str) -> None:
    """Fan out membership change to every bundled connector. Best-effort:
    a connector failure logs but doesn't block the membership change."""
    import logging
    log = logging.getLogger("yorik.spaces")
    for name, mod_path in (("paperless", "paperless_provisioning"),
                            ("immich",    "immich_provisioning")):
        try:
            mod = __import__(f"backend.{mod_path}", fromlist=["sync_space"])
            mod.sync_space(space_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "%s sync after %s failed (space=%s user=%s): %s",
                name, action, space_id, user_id, exc,
            )
