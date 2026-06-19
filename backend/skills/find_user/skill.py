"""find_user skill — look up household members by name/email.

Reads from user_profiles. Read-only; needs no special privileges. The
LLM uses this to resolve "Sara" → user_id before passing
attendee_user_ids to add_calendar_event, and to confirm that a named
person does or doesn't exist before asserting either way.
"""
from __future__ import annotations

from typing import Any, Optional


# Columns we surface to the LLM. Deliberately narrow — no password
# hash, no session info, no internal flags. Just what's needed to act
# on a user (the id) and to display them to the human.
_PUBLIC_COLS = ("id", "role", "name", "first_name", "last_name", "email")


async def execute(
    ctx,
    query: Optional[str] = None,
    role: Optional[str] = None,
) -> dict[str, Any]:
    q = (query or "").strip()
    role_filter = (role or "").strip().lower() or None

    from backend.database import get_conn

    sql_parts = [f"SELECT {', '.join(_PUBLIC_COLS)} FROM user_profiles"]
    where: list[str] = []
    params: list[Any] = []

    if q:
        # Case-insensitive substring on any of the name-ish fields.
        # SQLite's LIKE is case-insensitive for ASCII by default; we
        # lower() both sides for non-ASCII robustness (Sara vs ANNA).
        like = f"%{q.lower()}%"
        where.append(
            "(LOWER(COALESCE(first_name, '')) LIKE ? "
            " OR LOWER(COALESCE(last_name, '')) LIKE ? "
            " OR LOWER(COALESCE(name, ''))       LIKE ? "
            " OR LOWER(COALESCE(email, ''))      LIKE ?)"
        )
        params.extend([like, like, like, like])

    if role_filter:
        where.append("LOWER(role) = ?")
        params.append(role_filter)

    # Phase C T13 audit fix: workspace-scoping.
    # Without this, Jane (WS3 admin) chatting "show me my contacts"
    # would see every user_profile across all workspaces as
    # "household members". Scope to user_profiles whose user shares
    # at least one workspace with the caller — either as owner or
    # via space_members. platform_admin sees everyone; an
    # anonymous/no-ctx caller (internal tooling) keeps the old
    # behaviour.
    caller_id = getattr(ctx, "user_id", None) if ctx else None
    caller_role = getattr(ctx, "role", None) if ctx else None
    if caller_id is not None and caller_role != "platform_admin":
        where.append(
            "id IN ("
            " SELECT ? "
            " UNION "
            " SELECT w.owner_user_id FROM workspaces w "
            "   WHERE w.id IN ("
            "     SELECT id FROM workspaces WHERE owner_user_id = ? "
            "     UNION "
            "     SELECT s.workspace_id FROM spaces s "
            "     JOIN space_members sm ON sm.space_id = s.id "
            "     WHERE sm.user_id = ?"
            "   ) "
            " UNION "
            " SELECT sm2.user_id FROM space_members sm2 "
            "   JOIN spaces s2 ON s2.id = sm2.space_id "
            "   WHERE s2.workspace_id IN ("
            "     SELECT id FROM workspaces WHERE owner_user_id = ? "
            "     UNION "
            "     SELECT s3.workspace_id FROM spaces s3 "
            "     JOIN space_members sm3 ON sm3.space_id = s3.id "
            "     WHERE sm3.user_id = ?"
            "   )"
            ")"
        )
        params.extend([caller_id, caller_id, caller_id,
                       caller_id, caller_id])

    if where:
        sql_parts.append("WHERE " + " AND ".join(where))
    sql_parts.append("ORDER BY id ASC LIMIT 50")
    sql = " ".join(sql_parts)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    users = [dict(r) for r in rows]

    result: dict[str, Any] = {"users": users, "count": len(users)}

    if not users:
        result["_llm_hint"] = (
            f"No Yorik user matches query={q!r} role={role_filter!r}. "
            "This is an HONEST not-found, not a tool failure — quote it "
            "to the user as 'X hat kein Yorik-Konto' and (if relevant) "
            "offer to add them as a household member."
        )
    elif len(users) > 1 and q:
        result["_llm_hint"] = (
            f"{len(users)} users matched. If you need a single id (e.g. "
            "for attendee_user_ids), ASK the user which one — quote the "
            "display names and roles. Never guess."
        )
    return result
