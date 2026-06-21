"""propose_meeting_slot suggestion type.

Emitted ONLY when the incoming message EXPLICITLY proposes a
date/time OR explicitly asks to schedule. Conservative on purpose
— the user does not want a meeting card popping up on every "we
should catch up" mention. The system prompt enforces this; the
validate hook below adds a second safety net (no slot that
overlaps an existing event).

On Accept the handler inserts an `events` row directly. The
calendar UI picks it up via its normal range query. We do NOT go
through the add_calendar_event skill — that's the LLM tool path
with its own confirmation dance; here the user already confirmed
by clicking Accept."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from ..registry import HandlerContext, SuggestionType, register_type

log = logging.getLogger("yorik.suggestions.types.propose_meeting_slot")


PAYLOAD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["title", "starts_at", "ends_at"],
    "properties": {
        "title":      {"type": "string", "description": "Event title, e.g. 'Coffee with Anna'.", "maxLength": 200},
        "starts_at":  {"type": "string", "description": "ISO 8601 local datetime, e.g. '2026-06-25T14:00:00'."},
        "ends_at":    {"type": "string", "description": "ISO 8601 local datetime, must be after starts_at."},
        "location":   {"type": "string", "description": "Optional venue / address.", "maxLength": 200},
        "all_day":    {"type": "boolean", "description": "True for date-only proposals.", "default": False},
    },
}


_ISO_FMTS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip().replace("Z", "")
    for fmt in _ISO_FMTS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


async def _validate(payload: Dict[str, Any], ctx: HandlerContext) -> bool:
    """Drop the suggestion if the proposed slot overlaps an
    existing event. Belt-and-braces — the LLM is told to check
    calendar context in evidence, but the model can hallucinate;
    this is a hard check against the DB.
    """
    start = _parse_iso(payload.get("starts_at"))
    end = _parse_iso(payload.get("ends_at"))
    if not start or not end or end <= start:
        log.info("propose_meeting_slot dropped: bad time range %r → %r",
                 payload.get("starts_at"), payload.get("ends_at"))
        return False

    from ...database import get_conn
    with get_conn() as conn:
        # Overlap: existing.starts_at < new.ends_at AND existing.ends_at > new.starts_at
        row = conn.execute(
            "SELECT id, title FROM events "
            "WHERE owner_user_id=? AND all_day=0 "
            "  AND starts_at < ? AND ends_at > ? LIMIT 1",
            (ctx.owner_user_id, end.isoformat(), start.isoformat()),
        ).fetchone()
    if row:
        log.info("propose_meeting_slot dropped: overlaps event #%s (%s)",
                 row["id"], row["title"])
        return False
    return True


async def _handle(payload: Dict[str, Any], ctx: HandlerContext) -> Dict[str, Any]:
    title = (payload.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "empty title"}
    start = _parse_iso(payload.get("starts_at"))
    end = _parse_iso(payload.get("ends_at"))
    if not start or not end:
        return {"ok": False, "error": "bad starts_at/ends_at"}
    all_day = bool(payload.get("all_day"))
    location = (payload.get("location") or "").strip() or None

    from ...database import get_conn
    with get_conn() as conn:
        cal = conn.execute(
            "SELECT id FROM calendars WHERE owner_user_id=? "
            "ORDER BY is_primary DESC NULLS LAST, id ASC LIMIT 1",
            (ctx.owner_user_id,),
        ).fetchone()
        calendar_id = int(cal["id"]) if cal else None

        person = None
        if ctx.contact_id:
            cr = conn.execute(
                "SELECT display_name FROM contacts WHERE id=?", (ctx.contact_id,),
            ).fetchone()
            person = cr["display_name"] if cr else None

        cur = conn.execute(
            "INSERT INTO events (title, starts_at, ends_at, all_day, person, "
            "  notes, calendar_id, owner_user_id, visibility, location) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (title, start.isoformat(), end.isoformat(),
             1 if all_day else 0, person,
             f"From Yorik suggestion #{ctx.suggestion_id}", calendar_id,
             ctx.owner_user_id, "private", location),
        )
        event_id = int(cur.fetchone()["id"])
        conn.commit()

    log.info("propose_meeting_slot accepted: suggestion=%s → event=%s",
             ctx.suggestion_id, event_id)
    return {"ok": True, "event_id": event_id, "action": "event_created"}


register_type(SuggestionType(
    type="propose_meeting_slot",
    payload_schema=PAYLOAD_SCHEMA,
    handler=_handle,
    validate=_validate,
    fallback_title="Schedule meeting",
))
