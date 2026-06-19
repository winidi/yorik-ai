"""
Calendars module — collection + ACL + invitation primitives.

What lives here:

  * CRUD on calendars + calendar_shares (the "who can see what" layer).
  * `can_access(user, calendar, level)` — the single function every
    endpoint and skill consults before rendering an event.
  * `visible_calendars_for(user)` — the list a user's CalendarApp loads
    into its sidebar.
  * `auto_route_calendar(creator_user, attendee_user_ids)` — when the
    user (or LLM) creates an event, pick the most likely calendar
    based on who's involved: multi-user → Shared, solo → Personal.
  * `freebusy(user_ids, from_iso, to_iso, requested_by)` — opaque time
    blocks across users, the meeting-scheduler primitive.
  * Attendees: add / RSVP / propose new time.

What does NOT live here:

  * Event create/edit/delete — that stays in the existing events
    module / REST endpoints. We expose helpers but don't reshuffle
    that surface.
  * Notification dispatch on invite — main.py wires it via
    notifications module after calling `add_attendee`.

ACL model (3 levels):
  - free_busy → caller sees opaque "Busy" blocks, no titles
  - read     → caller sees full event details
  - write    → caller can create/edit/delete events on this calendar

Per-event override: events.visibility='private' downgrades reads to
free_busy for non-owners even on calendars they have read on. This is
the "even though Mum can see my Personal calendar, hide *this specific*
therapy appointment" escape hatch.

Admin pass-through: users with role='admin' implicitly get 'read' on
every calendar unless that calendar has hide_from_admin=1.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .database import conn_ctx, get_conn

log = logging.getLogger(__name__)


ACCESS_LEVELS = ("free_busy", "read", "write")
_LEVEL_ORDER = {"free_busy": 1, "read": 2, "write": 3}


# ─── Lookup helpers ───────────────────────────────────────────────────


def get(calendar_id: int) -> Optional[Dict[str, Any]]:
    with conn_ctx() as c:
        row = c.execute(
            "SELECT * FROM calendars WHERE id = ? AND archived_at IS NULL",
            (calendar_id,),
        ).fetchone()
    return dict(row) if row else None


def list_all(*, include_archived: bool = False) -> List[Dict[str, Any]]:
    where = "" if include_archived else "WHERE archived_at IS NULL"
    with conn_ctx() as c:
        rows = c.execute(
            f"SELECT * FROM calendars {where} ORDER BY kind, id"
        ).fetchall()
    return [dict(r) for r in rows]


def list_personal_for(user_id: str) -> List[Dict[str, Any]]:
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT * FROM calendars WHERE owner_user_id = ? AND kind = 'personal' "
            "AND archived_at IS NULL ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def shared_calendar() -> Optional[Dict[str, Any]]:
    """The household/business-wide 'Shared' calendar (kind='shared')."""
    with conn_ctx() as c:
        row = c.execute(
            "SELECT * FROM calendars WHERE kind = 'shared' "
            "AND archived_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def visible_calendars_for(user_id: str, user_role: str) -> List[Dict[str, Any]]:
    """Every calendar this user can see, with their effective access level
    on each. Powers the sidebar in CalendarApp.
    """
    out: List[Dict[str, Any]] = []
    for cal in list_all():
        level = effective_access(user_id, user_role, cal)
        if level:
            out.append({**cal, "access_level": level, "you_own": cal["owner_user_id"] == user_id})
    return out


# ─── ACL ──────────────────────────────────────────────────────────────


def _explicit_share(calendar_id: int, user_id: str) -> Optional[str]:
    """Per-calendar share lookup. Phase B (calendar_shares table dropped):
    a user has "explicit share" access via space membership on the
    calendar's space. Mapping: space write/admin → 'write', read → 'read'.
    Kept as a helper because effective_access still wants the answer
    decoupled from owner / admin overlays.
    """
    with conn_ctx() as c:
        row = c.execute("SELECT space_id FROM calendars WHERE id=?", (calendar_id,)).fetchone()
    if not row or row["space_id"] is None:
        return None
    from . import spaces as _sp
    level = _sp.user_space_level(user_id, int(row["space_id"]))
    if level == "read":
        return "read"
    if level in ("write", "admin"):
        return "write"
    return None


def effective_access(user_id: str, user_role: str, calendar: Dict[str, Any]) -> Optional[str]:
    """Return the user's effective access level on this calendar, or None
    if they cannot see it. Phase B: the calendar's space membership
    decides it. Resolution order:

      1. Owner of the calendar → 'write' (always wins).
      2. Admin pass-through unless calendar.hide_from_admin.
      3. Space membership on calendar.space_id mapped to a level.
      4. None.

    Space level mapping (no free_busy sub-level for now — Phase C work):
      space write/admin → calendar 'write'
      space read        → calendar 'read'
      not a member      → None
    """
    if calendar["owner_user_id"] == user_id:
        return "write"
    role_l = (user_role or "").lower()
    if role_l == "platform_admin" and not calendar.get("hide_from_admin"):
        return "read"
    if role_l == "admin" and not calendar.get("hide_from_admin"):
        # Workspace admin pass-through: only for calendars in workspaces
        # this user owns. Falls through to space membership otherwise.
        from . import database as _db
        with _db.conn_ctx() as _c:
            owns = _c.execute(
                "SELECT 1 FROM workspaces w "
                "JOIN spaces s ON s.workspace_id = w.id "
                "WHERE s.id = ? AND w.owner_user_id = ?",
                (calendar.get("space_id"), user_id),
            ).fetchone()
        if owns:
            return "read"
    space_id = calendar.get("space_id")
    if space_id is not None:
        from . import spaces as _sp
        level = _sp.user_space_level(user_id, int(space_id), user_role)
        if level == "read":
            return "read"
        if level in ("write", "admin"):
            return "write"
    return None


class EventPermissionError(PermissionError):
    """Raised by require_event_owner_or_admin when the caller is neither
    the event's owner nor an admin. Kept as a distinct subclass so the
    skill layer can catch it and surface a friendly message without
    swallowing generic PermissionError from elsewhere."""


class RowOwnerPermissionError(PermissionError):
    """Generic version of EventPermissionError for any row with an
    owner_user_id column (tasks, bills, anything with the same shape)."""


def require_row_owner_or_admin(
    role: Optional[str],
    user_id: Optional[int],
    row: Dict[str, Any],
    *,
    subject: str = "row",
    owner_col: str = "owner_user_id",
) -> None:
    """Gate mutation skills so a non-admin member can only act on rows
    THEY own. Shared by the calendar event skills, the task skills, and
    anything else with a per-row ownership column.

    Admin always allowed. Legacy rows with no owner_user_id are gated
    to admin too — safer than guessing.

    `subject` is the noun used in error messages ("event", "task",
    etc.) so the LLM relays a sensible message to the user.
    `owner_col` lets tasks reuse this helper without a schema rename
    (they store creator id under `created_by_user_id`).
    """
    if (role or "").strip().lower() == "admin":
        return
    owner = row.get(owner_col)
    if owner is None:
        raise RowOwnerPermissionError(
            f"this {subject} has no recorded owner (legacy row); only "
            f"an admin can change it."
        )
    # Phase E: user ids are UUIDs (strings), not ints. Compare as
    # strings so workspace_admin / personal-space ownership still
    # gates correctly. The historical int() cast crashed with
    # 'invalid literal for int() with base 10' on every chat-driven
    # update_task / update_event call after Phase E.
    if user_id is None or str(user_id) != str(owner):
        raise RowOwnerPermissionError(
            f"only the {subject}'s owner can change it. Ask the owner "
            f"(or an admin) to make the change for you."
        )


def require_contact_access(
    role: Optional[str],
    user_id: Optional[int],
    contact_row: Dict[str, Any],
    *,
    action: str = "edit",
) -> None:
    """Gate mutation/read on contacts via Phase B's spaces model. Caller
    is allowed if ANY of:

      - role is 'admin' (admin always wins)
      - caller owns the contact (created_by_user_id)
      - caller is a member of the contact's space at the required level
        (any level for view; write/admin for edit)
      - row_shares entry exists (read for view; write for edit)

    Raises RowOwnerPermissionError otherwise. The legacy allowed_roles
    + contact_shares paths are gone (Phase B.1 cutover, 2026-06-02);
    backfill in migration 036 ensured every row carries a space_id.

    `action` is 'view' or 'edit' — picks the level threshold.
    """
    from backend import spaces as _sp
    if action == "edit":
        if _sp.can_write_row(user_id, role, "contacts", contact_row):
            return
    else:
        if _sp.can_view_row(user_id, role, "contacts", contact_row):
            return
    raise RowOwnerPermissionError(
        f"only the contact's owner, a member of its space, or someone "
        f"explicitly shared with can change it."
    )


def require_event_owner_or_admin(
    role: Optional[str], user_id: Optional[int], event_row: Dict[str, Any],
) -> None:
    """Calendar-event-specific wrapper around require_row_owner_or_admin.
    Kept as a named function for the explicit semantics + because the
    delete_calendar_event / update_calendar_event / block_travel_time
    skills already import it under this name.

    Raises EventPermissionError (subclass of PermissionError) so existing
    catch-by-class call sites keep working — we re-wrap the generic
    error into the event-specific one.
    """
    try:
        require_row_owner_or_admin(role, user_id, event_row, subject="event")
    except RowOwnerPermissionError as e:
        raise EventPermissionError(str(e)) from e


def can_access(
    user_id: str, user_role: str, calendar: Dict[str, Any], need: str,
) -> bool:
    """Does this user have AT LEAST the requested access level?"""
    if need not in _LEVEL_ORDER:
        raise ValueError(f"unknown access level: {need!r}")
    eff = effective_access(user_id, user_role, calendar)
    if not eff:
        return False
    return _LEVEL_ORDER[eff] >= _LEVEL_ORDER[need]


def visible_event_filter(user_id: str, user_role: str) -> tuple[str, list[Any]]:
    """Build a SQL WHERE-clause + params for events the given user can
    see. Returns (sql_fragment, params) appended as `AND (sql_fragment)`
    to an events query.

    Phase B model (2026-06-02): events inherit visibility through their
    calendar's space membership. Plus event_attendees as an orthogonal
    invitation channel (an invite grants visibility even on calendars
    you're not a member of).
    """
    from . import spaces as _sp
    parts: list[str] = []
    params: list[Any] = []

    r = (user_role or "").lower()
    if r == "platform_admin":
        # Platform admin (infra operator) sees every calendar that
        # isn't explicitly hidden, plus orphan events. Today's pre-T10
        # `admin` behaviour preserved on this role.
        parts.append(
            "events.calendar_id IN (SELECT id FROM calendars WHERE hide_from_admin = 0)"
        )
        parts.append("events.calendar_id IS NULL")
    else:
        # admin (workspace admin), member, restricted, etc. all flow
        # through space visibility now. `user_visible_space_ids` returns
        # the workspace-scoped set for `admin`, full set for platform_admin
        # (handled above), personal+memberships for everyone else.
        visible_spaces = _sp.user_visible_space_ids(user_id, user_role)
        if visible_spaces:
            placeholders = ",".join("?" * len(visible_spaces))
            parts.append(
                f"events.calendar_id IN "
                f"(SELECT id FROM calendars WHERE space_id IN ({placeholders}))"
            )
            params.extend(visible_spaces)
        # Invitation visibility — orthogonal to space membership.
        parts.append("events.id IN (SELECT event_id FROM event_attendees WHERE user_id = ?)")
        params.append(user_id)
    if not parts:
        return "1=0", []
    return "(" + " OR ".join(parts) + ")", params


def downgrade_for_privacy(
    event_row: Dict[str, Any], user_id: str, user_role: str,
    calendar: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Apply per-event + per-share privacy. Returns a copy of the event
    dict with title/notes blanked when the user only has free_busy or
    when visibility='private' and the user isn't the owner.
    """
    if calendar is None:
        calendar = get(int(event_row["calendar_id"])) if event_row.get("calendar_id") else None
    if calendar is None:
        return event_row

    eff = effective_access(user_id, user_role, calendar)
    is_private = event_row.get("visibility") == "private"
    is_owner = event_row.get("owner_user_id") == user_id

    # Strongest privacy rule: private event seen by non-owner → busy only.
    if is_private and not is_owner:
        return _busy_only(event_row)
    if eff == "free_busy":
        return _busy_only(event_row)
    return event_row


def _busy_only(event_row: Dict[str, Any]) -> Dict[str, Any]:
    redacted = dict(event_row)
    redacted["title"]  = "Busy"
    redacted["notes"]  = None
    redacted["person"] = None
    redacted["_busy_only"] = True
    return redacted


# ─── Create / share ───────────────────────────────────────────────────


def create_calendar(
    *,
    name: str,
    owner_user_id: str,
    color: str = "#a78bfa",
    kind: str = "personal",
    hide_from_admin: bool = False,
) -> int:
    name = (name or "").strip() or "Untitled"
    # Phase B: every calendar must live in a space. Personal calendars
    # land in the owner's personal space; shared calendars in Household.
    from . import spaces as _sp
    space_id: Optional[int] = None
    if kind == "personal":
        space_id = _sp.personal_space_id(owner_user_id)
    else:
        with conn_ctx() as _c:
            row = _c.execute(
                "SELECT id FROM spaces WHERE slug='household' LIMIT 1"
            ).fetchone()
            space_id = int(row["id"]) if row else None
    with conn_ctx() as c:
        cur = c.execute(
            "INSERT INTO calendars (name, color, owner_user_id, kind, hide_from_admin, space_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, color, owner_user_id, kind, 1 if hide_from_admin else 0, space_id),
        )
        return int(cur.lastrowid)


def rename_calendar(calendar_id: int, *, name: Optional[str] = None,
                    color: Optional[str] = None,
                    hide_from_admin: Optional[bool] = None) -> None:
    sets: list[str] = []
    params: list[Any] = []
    if name is not None:
        sets.append("name = ?"); params.append(name.strip() or "Untitled")
    if color is not None:
        sets.append("color = ?"); params.append(color)
    if hide_from_admin is not None:
        sets.append("hide_from_admin = ?"); params.append(1 if hide_from_admin else 0)
    if not sets:
        return
    params.append(calendar_id)
    with conn_ctx() as c:
        c.execute(f"UPDATE calendars SET {', '.join(sets)} WHERE id = ?", params)


def archive_calendar(calendar_id: int) -> None:
    with conn_ctx() as c:
        c.execute(
            "UPDATE calendars SET archived_at = datetime('now') WHERE id = ?",
            (calendar_id,),
        )


# Personal-calendar palette — mirrors the rotation in migration 010 so
# calendars created here look identical to migration-seeded ones.
# Personal-calendar color cycle. Picked so each hue is visually distinct
# from every event-category color (see frontend/.../categoryPalette.ts:
# family=emerald, business=slate, drive=amber, health=rose, personal=
# violet, social=sky). That way, when a calendar's color shows up as the
# fill on a SHARED event (i.e. an event from someone else's calendar
# overlayed on your view), you can tell at a glance it's not a category
# color — it's whose calendar it is.
#
# 0 → fuchsia, 1 → indigo, 2 → lime, 3 → orange, 4 → cyan. Cycle by
# (user_id % 5).
_PERSONAL_PALETTE = ("#d946ef", "#6366f1", "#84cc16", "#f97316", "#22d3ee")

# Shared / household calendar color. Distinct from both the category
# palette AND the personal palette so it has its own visual identity.
_SHARED_COLOR = "#ec4899"  # pink-500


def ensure_calendars_for_user(user_id: str, user_name: Optional[str]) -> None:
    """Provision the calendars a new user needs to save events.

    Called from every user-creation entry point (first-run admin setup
    in main.auth_setup, admin-driven users.create_user). Idempotent.

    Migration 010 only seeded calendars for users that existed AT
    MIGRATION TIME — on a fresh install the migration runs against an
    empty DB and seeds nothing, so without this helper the first
    add_calendar_event raises "no calendars exist".
    """
    if not list_personal_for(user_id):
        # Phase E user ids are UUID strings; pick a stable palette
        # slot from the hashed bytes so repeated calls land on the
        # same color. Pre-Phase-E integer ids fall through to the
        # int branch.
        slot = (user_id if isinstance(user_id, int)
                else int.from_bytes(str(user_id).encode("utf-8")[:8], "big"))
        color = _PERSONAL_PALETTE[slot % len(_PERSONAL_PALETTE)]
        label = (user_name or "").strip() or "My"
        create_calendar(
            name=f"{label}'s calendar",
            owner_user_id=user_id,
            color=color,
            kind="personal",
        )

    shared = shared_calendar()
    if not shared:
        with conn_ctx() as c:
            admin_row = c.execute(
                "SELECT id FROM user_profiles WHERE role = 'admin' "
                "ORDER BY id LIMIT 1"
            ).fetchone()
        if admin_row:
            create_calendar(
                name="Shared",
                owner_user_id=int(admin_row["id"]),
                color=_SHARED_COLOR,
                kind="shared",
            )
            shared = shared_calendar()

    # Phase B: don't auto-share the Shared calendar here. Household
    # membership is the source of truth, and the level is role-aware
    # (members get 'write', restricted users get 'read') — set by the
    # caller (users.create_user / main.auth_setup). The legacy upsert_
    # share call would race INSERT-OR-IGNORE against the caller's
    # role-aware add and pin everyone to 'write'.


def upsert_share(calendar_id: int, user_id: str, access_level: str) -> None:
    """Phase B: share a calendar by adding the user to its space.
    `access_level` ('read' / 'write') maps to space level ('read' / 'write').
    Resolves the calendar's space_id; raises if the calendar doesn't have
    one (shouldn't happen post-migration-036)."""
    if access_level not in ACCESS_LEVELS:
        raise ValueError(f"access_level must be one of {ACCESS_LEVELS}")
    with conn_ctx() as c:
        row = c.execute("SELECT space_id FROM calendars WHERE id=?", (calendar_id,)).fetchone()
        if not row:
            raise ValueError(f"calendar {calendar_id} not found")
        space_id = row["space_id"]
        if space_id is None:
            raise ValueError(f"calendar {calendar_id} has no space (migration 036 missed?)")
        # 'write' on calendar → 'write' in space; 'read' / 'free_busy' → 'read'.
        space_level = "write" if access_level == "write" else "read"
        c.execute(
            "INSERT OR REPLACE INTO space_members "
            "(space_id, user_id, level) VALUES (?, ?, ?)",
            (int(space_id), user_id, space_level),
        )
    from . import spaces as _sp
    _sp.on_space_member_added(int(space_id), user_id, space_level)  # type: ignore[arg-type]


def remove_share(calendar_id: int, user_id: str) -> None:
    with conn_ctx() as c:
        row = c.execute("SELECT space_id FROM calendars WHERE id=?", (calendar_id,)).fetchone()
        if not row or row["space_id"] is None:
            return
        space_id = int(row["space_id"])
        c.execute(
            "DELETE FROM space_members WHERE space_id=? AND user_id=?",
            (space_id, user_id),
        )
    from . import spaces as _sp
    _sp.on_space_member_removed(space_id, user_id)


def list_shares(calendar_id: int) -> List[Dict[str, Any]]:
    """All non-owner members of this calendar's space, joined with user
    profile for UI rendering. Phase B: shares are now space membership."""
    with conn_ctx() as c:
        row = c.execute(
            "SELECT space_id, owner_user_id FROM calendars WHERE id=?",
            (calendar_id,),
        ).fetchone()
        if not row or row["space_id"] is None:
            return []
        # owner_user_id is a UUID string on Phase E Postgres; the old
        # `int(row["owner_user_id"])` coercion crashed with
        # `invalid literal for int() with base 10: '<uuid>'`. Pass the
        # value through as-is — sm.user_id is also UUID on Phase E so
        # the `sm.user_id != ?` comparison is type-clean. A sentinel
        # empty string is enough to never match a real UUID, so the
        # `owner_user_id IS NULL` case still works.
        owner_filter = row["owner_user_id"] if row["owner_user_id"] else ""
        rows = c.execute(
            "SELECT sm.user_id, sm.level AS access_level, sm.added_at AS created_at, "
            "       u.name, u.email, u.role "
            "FROM space_members sm JOIN user_profiles u ON u.id = sm.user_id "
            "WHERE sm.space_id = ? AND sm.user_id != ? "
            "ORDER BY sm.added_at DESC",
            (int(row["space_id"]), owner_filter),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Auto-route on event create ───────────────────────────────────────


def auto_route_calendar(
    creator_user_id: str, attendee_user_ids: Sequence[int],  # noqa: ARG001
) -> int:
    """Pick the default calendar for a new event. The rule is dead
    simple: ALWAYS the creator's Personal calendar.

    Earlier versions routed multi-attendee events to the Household
    calendar, which felt clever but was wrong: a 'lunch with wife'
    is a thing on MY calendar (and also on wife's via the attendee
    join). Routing it to Household made it nobody's, and meant the
    bucket filled up with normal day-to-day events that should have
    stayed personal. Multi-attendee events show up on every attendee's
    view via event_attendees regardless of which calendar they live
    on, so there's no need to put them anywhere shared.

    Household is reserved for genuinely person-agnostic items
    (trash day, mortgage, holidays). Picking Household requires an
    explicit `calendar_id=` argument from the caller (UI dropdown
    or LLM skill).

    Falls back to the Household calendar only when the creator has
    no personal calendar — shouldn't happen post-migration but the
    fallback avoids a 500.
    """
    personal = list_personal_for(creator_user_id)
    if personal:
        return int(personal[0]["id"])
    shared = shared_calendar()
    if shared:
        return int(shared["id"])
    raise RuntimeError(
        f"no calendars exist for user {creator_user_id} — "
        "ensure_calendars_for_user() should run on signup"
    )


# ─── Attendees ────────────────────────────────────────────────────────


def add_attendees(
    event_id: int,
    *,
    user_ids: Sequence[int] = (),
    person_names: Sequence[str] = (),
) -> int:
    """Attach attendees to an event. Returns count inserted. Idempotent
    on (event_id, user_id) and (event_id, lower(person_name))."""
    inserted = 0
    with conn_ctx() as c:
        # User attendees
        for uid in user_ids:
            if not uid:
                continue
            try:
                c.execute(
                    "INSERT INTO event_attendees (event_id, user_id, response_status) "
                    "VALUES (?, ?, 'needs_action')",
                    (event_id, uid),
                )
                inserted += 1
            except Exception:
                # Likely UNIQUE collision — skip silently (idempotent).
                pass
        # Free-text attendees (kids without logins, external names)
        existing = {
            (r["person_name"] or "").strip().lower()
            for r in c.execute(
                "SELECT person_name FROM event_attendees "
                "WHERE event_id = ? AND person_name IS NOT NULL",
                (event_id,),
            ).fetchall()
        }
        for raw in person_names:
            name = (raw or "").strip()
            if not name or name.lower() in existing:
                continue
            c.execute(
                "INSERT INTO event_attendees (event_id, person_name, response_status) "
                "VALUES (?, ?, 'needs_action')",
                (event_id, name),
            )
            inserted += 1
    return inserted


def attendees_for(event_id: int) -> List[Dict[str, Any]]:
    """All attendees on an event, with user profile joined for accounts."""
    with conn_ctx() as c:
        rows = c.execute(
            "SELECT a.id, a.event_id, a.user_id, a.person_name, "
            "       a.response_status, a.proposed_time_iso, a.response_at, "
            "       u.name AS user_name, u.email AS user_email, u.role AS user_role "
            "FROM event_attendees a "
            "LEFT JOIN user_profiles u ON u.id = a.user_id "
            "WHERE a.event_id = ? "
            "ORDER BY a.id",
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def remove_attendee(attendee_id: int) -> None:
    with conn_ctx() as c:
        c.execute("DELETE FROM event_attendees WHERE id = ?", (attendee_id,))


def scan_overlaps(
    starts_at: str, ends_at: str, *,
    owner_user_id: str,
    exclude_event_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """List the user's events whose time window overlaps [starts_at, ends_at].

    Used by add_calendar_event and update_calendar_event to surface
    scheduling conflicts as a non-blocking warning ("you have Zahnarzt
    09:00-11:00 at that time — schedule anyway?"). Mirrors Google
    Calendar's behaviour: commit the event, then tell the user about
    the overlap so they can decide whether to reschedule.

    Anfahrt / Rückfahrt buffer events (notes LIKE '%[LINKED_TO=%') are
    excluded so the user's own travel block doesn't show up as a
    conflict against the event it's reserving time for.

    `exclude_event_id` skips the event itself — needed when called
    from update_calendar_event so a 9-10 → 9-11 extension doesn't
    flag itself as overlapping itself.
    """
    if not starts_at or not ends_at:
        return []
    # datetime() coerces both '2026-06-25T11:00' and '2026-06-25T11:00:00'
    # to the same canonical form — without it, lexicographic string compare
    # treats them as different and the boundary 11:00==11:00 false-positives
    # as an overlap.
    sql = (
        "SELECT id, title, starts_at, ends_at "
        "FROM events "
        "WHERE owner_user_id = ? "
        "  AND datetime(starts_at) < datetime(?) "
        "  AND datetime(ends_at)   > datetime(?) "
        "  AND (notes IS NULL OR notes NOT LIKE '%[LINKED_TO=%')"
    )
    params: list[Any] = [owner_user_id, ends_at, starts_at]
    if exclude_event_id is not None:
        sql += " AND id != ?"
        params.append(int(exclude_event_id))
    sql += " ORDER BY starts_at ASC LIMIT 5"
    with conn_ctx() as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def rsvp(
    event_id: int, user_id: str, *,
    status: str, proposed_time_iso: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Set the user's RSVP on an event. Returns the updated attendee
    row, or None if the user isn't an attendee."""
    if status not in ("accepted", "declined", "tentative"):
        raise ValueError("status must be accepted | declined | tentative")
    with conn_ctx() as c:
        row = c.execute(
            "SELECT id FROM event_attendees WHERE event_id = ? AND user_id = ?",
            (event_id, user_id),
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE event_attendees "
            "SET response_status = ?, proposed_time_iso = ?, "
            "    response_at = datetime('now') "
            "WHERE id = ?",
            (status, proposed_time_iso, row["id"]),
        )
        after = c.execute(
            "SELECT * FROM event_attendees WHERE id = ?", (row["id"],)
        ).fetchone()
    return dict(after) if after else None


def propose_time(event_id: int, user_id: str, new_time_iso: str) -> Optional[Dict[str, Any]]:
    """Counter-proposal — the attendee says 'I'll come, but at this
    other time'. Stored on their row; inviter sees both times."""
    return rsvp(event_id, user_id, status="tentative", proposed_time_iso=new_time_iso)


# ─── Free / busy aggregator ───────────────────────────────────────────


def freebusy(
    user_ids: Sequence[Any],
    from_iso: str,
    to_iso: str,
    *,
    requested_by_user_id: str,
    requested_by_role: str,
) -> Dict[Any, List[Dict[str, Any]]]:
    """Return opaque busy blocks per user across the requested window.

    Respects privacy: anything the requester wouldn't be allowed to even
    see at the `free_busy` level is silently dropped from the result.
    No titles, no notes — just `{start, end}` blocks. The meeting-time
    finder UI stacks these to highlight overlaps.

    Includes events from any calendar each user owns OR is an attendee
    on (so an event invite from someone else counts as that user being
    busy). Dedupes overlapping windows per user.

    `user_ids` accepts UUID strings (Phase E) OR ints (pre-Phase-E).
    The result key matches whatever the caller passed.
    """
    if not user_ids:
        return {}

    result: Dict[Any, List[Dict[str, Any]]] = {u: [] for u in user_ids}

    with conn_ctx() as c:
        for uid in user_ids:
            uid = uid
            # All calendars this user owns
            calendars = c.execute(
                "SELECT id, hide_from_admin FROM calendars "
                "WHERE owner_user_id = ? AND archived_at IS NULL",
                (uid,),
            ).fetchall()
            cal_ids = [int(row["id"]) for row in calendars]

            # Filter out calendars the requester cannot see at all. The
            # requester is the OWNER of their own calendars (they can
            # always see their own free/busy). For others, we still
            # expose busy blocks unless hide_from_admin is on AND the
            # requester isn't admin … wait, hide_from_admin specifically
            # blocks admin. For non-admin requesters, the rule is
            # "user-to-user free/busy is always allowed unless the
            # calendar isn't shared at all." Implementation: if there's
            # NO share row for the requester on a calendar AND the
            # requester isn't the owner AND the requester isn't admin
            # (or hide_from_admin is set), drop the calendar.
            allowed_cal_ids: list[int] = []
            for cal in calendars:
                cid = int(cal["id"])
                if uid == requested_by_user_id:
                    allowed_cal_ids.append(cid)
                    continue
                # explicit share?
                share = c.execute(
                    "SELECT 1 FROM calendar_shares WHERE calendar_id = ? AND user_id = ?",
                    (cid, requested_by_user_id),
                ).fetchone()
                if share:
                    allowed_cal_ids.append(cid)
                    continue
                if requested_by_role == "admin" and not cal["hide_from_admin"]:
                    allowed_cal_ids.append(cid)

            blocks: list[Dict[str, Any]] = []
            if allowed_cal_ids:
                placeholders = ",".join("?" * len(allowed_cal_ids))
                rows = c.execute(
                    f"SELECT starts_at, ends_at FROM events "
                    f"WHERE calendar_id IN ({placeholders}) "
                    f"AND starts_at < ? AND COALESCE(ends_at, starts_at) > ?",
                    [*allowed_cal_ids, to_iso, from_iso],
                ).fetchall()
                blocks.extend(
                    {"start": r["starts_at"], "end": r["ends_at"] or r["starts_at"]}
                    for r in rows
                )

            # Also include events the user is an ATTENDEE on (could be on
            # someone else's calendar). Accepted + tentative + needs_action
            # all count as busy. Declined does not.
            attendee_rows = c.execute(
                "SELECT e.starts_at, e.ends_at "
                "FROM event_attendees a "
                "JOIN events e ON e.id = a.event_id "
                "WHERE a.user_id = ? "
                "  AND a.response_status IN ('accepted', 'tentative', 'needs_action') "
                "  AND e.starts_at < ? AND COALESCE(e.ends_at, e.starts_at) > ?",
                (uid, to_iso, from_iso),
            ).fetchall()
            blocks.extend(
                {"start": r["starts_at"], "end": r["ends_at"] or r["starts_at"]}
                for r in attendee_rows
            )

            # Merge overlapping blocks per user so the UI doesn't render
            # 6 stacked bars when the truth is one chunk.
            result[uid] = _merge_blocks(blocks)

    return result


def _merge_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not blocks:
        return []
    sorted_b = sorted(blocks, key=lambda b: b["start"])
    out: List[Dict[str, Any]] = [dict(sorted_b[0])]
    for b in sorted_b[1:]:
        last = out[-1]
        if b["start"] <= last["end"]:
            if b["end"] > last["end"]:
                last["end"] = b["end"]
        else:
            out.append(dict(b))
    return out
