"""Calendar retriever — events involving the contact in the next/past 14 days.

Uses event_attendees.person_name match (free-text — calendar attendees
aren't FK-linked to contacts.id yet). Falls back to events.person if
no attendee match. Returns up to 10 events.

Critical for the propose_meeting_slot suggestion type: lets the LLM
see "Anna's already on Tuesday at 14:00 — don't propose that slot."
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..registry import ContextRetriever, Evidence, RetrieverContext, register_retriever

WINDOW_DAYS_PAST = 14
WINDOW_DAYS_FUTURE = 14


async def _fetch(ctx: RetrieverContext) -> list[Evidence]:
    if ctx.contact_id is None:
        return []
    from ...database import get_conn
    with get_conn() as conn:
        name_row = conn.execute(
            "SELECT display_name FROM contacts WHERE id=?",
            (ctx.contact_id,),
        ).fetchone()
    if not name_row:
        return []
    name = (name_row["display_name"] or "").strip()
    if not name:
        return []

    now = datetime.now(timezone.utc)
    start_iso = (now - timedelta(days=WINDOW_DAYS_PAST)).isoformat()
    end_iso   = (now + timedelta(days=WINDOW_DAYS_FUTURE)).isoformat()

    from ...calendars import visible_event_filter
    with get_conn() as conn:
        # the owner's own calendars and shares, never another person's
        # private event (audit 2026-09-22, 3.6)
        role_row = conn.execute("SELECT role FROM user_profiles WHERE id = ?", (ctx.owner_user_id,)).fetchone()
        ev_sql, ev_params = visible_event_filter(str(ctx.owner_user_id), (role_row["role"] if role_row else "") or "")
        rows = conn.execute(
            "SELECT DISTINCT events.id, events.title, events.starts_at, events.ends_at "
            "FROM events "
            "LEFT JOIN event_attendees a ON a.event_id = events.id "
            "WHERE events.starts_at >= ? AND events.starts_at <= ? "
            "  AND ((LOWER(a.person_name) = LOWER(?)) "
            "       OR (LOWER(COALESCE(events.person,'')) = LOWER(?))) "
            f"  AND {ev_sql} AND (events.visibility IS DISTINCT FROM 'private' OR events.owner_user_id = ?) "
            "ORDER BY events.starts_at LIMIT 10",
            (start_iso, end_iso, name, name, *ev_params, ctx.owner_user_id),
        ).fetchall()

    out: list[Evidence] = []
    for r in rows:
        starts = (r["starts_at"] or "")[:16].replace("T", " ")
        snippet = f"{r['title']} · {starts}"
        out.append(Evidence(
            kind="calendar_event",
            ref_id=int(r["id"]),
            snippet=snippet[:140],
        ))
    return out


register_retriever(ContextRetriever(
    name="calendar",
    scope=["message", "contact"],
    fetch=_fetch,
))
