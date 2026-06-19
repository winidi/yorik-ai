"""Role-based filters for HomeOS.

Two layers:
  1. Typed REST endpoints (main.py) call `require_role` + `apply_filter` to build
     parameterized SQL the user is allowed to run.
  2. The Vanna agent (vanna_agent.py) uses `build_user_resolver(role)` so the
     tool registry gates which tools the role can invoke.

`filter_query_by_role` is a thin shim that satisfies the spec surface — it does
NOT rewrite SQL (string mutation is fragile), it only checks whether the SQL
references tables the role is not allowed to read.
"""

from __future__ import annotations

import re
from typing import Iterable

from fastapi import HTTPException

ROLES: set[str] = {"platform_admin", "admin", "member", "restricted", "child", "employee", "viewer"}

ALL_TABLES: set[str] = {
    "events", "tasks", "bills", "documents",
    "user_profiles", "saved_queries", "template_cache",
}

# Tables each role may READ (writes additionally require admin/member, enforced in main.py).
ROLE_TABLES: dict[str, set[str]] = {
    # platform_admin == global infra admin (sees every workspace's tables);
    # admin == workspace admin (same table set but space-filtered by
    # spaces.user_visible_space_ids).
    "platform_admin": set(ALL_TABLES),
    "admin":      set(ALL_TABLES),
    # Phase B (2026-06-02): role grants table-level access; spaces
    # decide which rows the user actually sees. Members can hit every
    # user-facing table; their row visibility narrows via
    # spaces.row_filter + per-row owner / row_shares.
    "member":     {"events", "tasks", "bills", "contacts", "documents",
                    "saved_queries", "calendars", "agent_conversations"},
    # Restricted is intentionally tighter: no bills (Finance space
    # membership won't help them — they're explicitly held back from
    # money + admin chores).
    "restricted": {"events", "tasks", "contacts", "agent_conversations"},
    # Legacy synonyms — Phase B migration 036 rewrites these to
    # 'restricted' in user_profiles.role. Kept here so any session
    # cookie issued before the migration still resolves to a valid
    # table-set on the first request post-deploy (it gets re-normalised
    # on next login).
    "child":      {"events", "tasks", "contacts"},
    "employee":   {"events", "tasks", "documents", "contacts"},
    "viewer":     {"events"},
}

# Phase B.2 (2026-06-02) — hard cutover: the per-table allowed_roles
# LIKE clauses are gone. Visibility is now decided by spaces (see
# backend/spaces.py). apply_filter remains in the codebase as a no-op
# shim so callers don't change shape; B.2.1 will remove the column +
# rename apply_filter into something more honest.
ROLE_FILTERS: dict[str, dict[str, str]] = {
    "platform_admin": {},
    "admin":      {},
    "member":     {},
    "restricted": {},
    "child":      {},
    "employee":   {},
    "viewer":     {},
}

# Phase B.x: restricted joined WRITE_ROLES so kids can create their
# OWN tasks, mark them done, RSVP to events, etc. Per-row cross-tenant
# safety stays — spaces.can_write_row gates which rows the restricted
# user actually has write access to (owner OR write-member-of-space).
# Without restricted here, a kid couldn't even check off their own
# chore. Restricted writes are still narrowed by:
#   1. ROLE_TABLES filters out bills entirely
#   2. space membership (kids default to 'read' on Household — see
#      users.add_user_to_household)
#   3. per-row owner gate (can't touch others' personal rows)
WRITE_ROLES: set[str] = {"platform_admin", "admin", "member", "restricted"}


def normalize_role(role: str | None) -> str:
    r = (role or "admin").lower().strip()
    if r not in ROLES:
        raise HTTPException(status_code=400, detail=f"Unknown role '{role}'. Allowed: {sorted(ROLES)}")
    return r


def require_role(role: str, table: str) -> None:
    r = normalize_role(role)
    if table not in ROLE_TABLES[r]:
        raise HTTPException(status_code=403, detail=f"Role '{r}' may not read table '{table}'")


def require_write(role: str) -> None:
    r = normalize_role(role)
    if r not in WRITE_ROLES:
        raise HTTPException(status_code=403, detail=f"Role '{r}' may not write")


def apply_filter(role: str, table: str, base_sql: str, params: tuple = ()) -> tuple[str, tuple]:
    """Append the role's WHERE fragment to `base_sql`.

    `base_sql` MUST end with either no WHERE clause or already contain one. We
    detect by case-insensitive scan and use AND vs WHERE accordingly.
    """
    r = normalize_role(role)
    extra = ROLE_FILTERS.get(r, {}).get(table)
    if not extra:
        return base_sql, params
    glue = " AND " if re.search(r"\bwhere\b", base_sql, re.IGNORECASE) else " WHERE "
    return f"{base_sql}{glue}{extra}", params


_TABLE_REF_RE = re.compile(r"\b(?:from|join|into|update)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def filter_query_by_role(sql: str, role: str) -> str:
    """Spec-mandated shim.

    Does not rewrite SQL. Raises PermissionError if the query references a table
    the role is not allowed to read. Returns the SQL unchanged otherwise.

    Admin bypasses the table list — they have read access to everything by
    design. ROLE_TABLES["admin"] used to be `set(ALL_TABLES)` but ALL_TABLES
    is just the original MVP table set; every migration added since (contacts
    in 008, calendars + calendar_shares + event_attendees in 010, etc.) silently
    became forbidden to admin too, which broke things like
    "find_contact → SELECT FROM contacts".
    """
    r = normalize_role(role)
    if r in ("platform_admin", "admin"):
        # platform_admin: true global bypass. admin: bypasses the table-name
        # check too — row visibility is enforced by spaces.row_filter for
        # workspace-scoping; this shim only gates which tables a role can
        # name at all.
        return sql
    referenced = {m.group(1).lower() for m in _TABLE_REF_RE.finditer(sql)}
    forbidden = referenced - ROLE_TABLES[r]
    if forbidden:
        raise PermissionError(f"Role '{r}' may not query table(s): {sorted(forbidden)}")
    return sql


def allowed_tools_for_role(role: str) -> Iterable[str]:
    """Role -> list of Vanna tool names the role may invoke."""
    r = normalize_role(role)
    # Everyone can read via the SQL tool; only admin / platform_admin get
    # the memory-write tools.
    base = ["run_sql"]
    if r in ("platform_admin", "admin"):
        base += ["save_question_tool_args", "save_text_memory", "search_saved_correct_tool_uses"]
    return base


def build_user_resolver(role: str):
    """Build a Vanna `UserResolver` that puts the given role in group_memberships.

    Imported lazily so importing this module does not require vanna installed.
    """
    from .agent.vanna_shim import RequestContext, User, UserResolver  # type: ignore

    r = normalize_role(role)

    class _FixedRoleResolver(UserResolver):
        async def resolve_user(self, request_context: RequestContext) -> User:  # type: ignore[override]
            return User(id=f"{r}@homeos.local", email=f"{r}@homeos.local", group_memberships=[r])

    return _FixedRoleResolver()
