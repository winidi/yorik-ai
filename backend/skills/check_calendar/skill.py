"""check_calendar skill — event lookup + optional free-slot computation."""

from __future__ import annotations
from datetime import datetime, timedelta, time as dtime
from typing import Any, Optional


WORK_START = dtime(8, 0)
WORK_END = dtime(22, 0)
MIN_SLOT_MIN = 30


async def execute(
    ctx,
    start_iso: Optional[str] = None,
    end_iso: Optional[str] = None,
    days: int = 7,
    title_contains: Optional[str] = None,
    include_free_slots: bool = False,
) -> dict[str, Any]:
    """Return events in a time window. Two robustness features for
    small-model date-math errors:

    1. `title_contains`: server-side title filter. When the LLM is
       looking for a specific event ("Zahnarzttermin"), passing this
       avoids the LLM filtering by title in its head — which is
       brittle when the LLM ALSO got the date wrong.

    2. `nearby` fallback: when the (filtered) window returns 0 events,
       we re-query a window widened by ±2 days. If those have hits,
       they're surfaced as `nearby` with a hint telling the LLM the
       requested date was probably wrong.
    """
    from backend.database import get_conn

    start = _parse_iso(start_iso) or datetime.now()
    end = _parse_iso(end_iso) or (start + timedelta(days=max(1, min(int(days), 30))))
    title_needle = (title_contains or "").strip().lower() or None

    user_id = getattr(ctx, "user_id", None)
    role = getattr(ctx, "role", None)

    events = _query_events(start, end, title_needle, user_id=user_id, role=role)

    nearby = None
    # Fallback: 0 hits in the requested window (after title filter) →
    # widen ±2 days AND keep the same title filter so we surface
    # plausible candidates ("you said Sunday but the Zahnarzt is
    # actually on Saturday").
    if not events:
        wider_start = start - timedelta(days=2)
        wider_end = end + timedelta(days=2)
        nearby_events = _query_events(wider_start, wider_end, title_needle,
                                      user_id=user_id, role=role)
        if nearby_events:
            nearby = {
                "events": nearby_events,
                "window": {
                    "start_iso": wider_start.isoformat(),
                    "end_iso":   wider_end.isoformat(),
                },
                "hint": (
                    "0 events matched in your requested window. These are within ±2 days "
                    "with the same title filter. If the user mentioned a weekday, you "
                    "probably computed the wrong ISO date — the events below are likely "
                    "the ones they meant. Use the corrected date going forward."
                ),
            }

    out: dict[str, Any] = {
        "events": events,
        "window": {"start_iso": start.isoformat(), "end_iso": end.isoformat()},
        "title_filter": title_needle,
    }
    if nearby:
        out["nearby"] = nearby
    if include_free_slots:
        out["free_slots"] = _compute_free_slots(events, start, end)

    # Anti-enumeration rule: even at small counts (3-4 events) the
    # audit caught the LLM listing every event with title + time in
    # prose. The card the chat renders IS the answer; prose listing
    # is noise. Always emit the hint when there's at least one event;
    # for the 0-events case let the LLM say so naturally.
    if events:
        # Inline events_found card mirrors tasks_found: clickable rows
        # so "what's on today?" renders the answer in chat instead of
        # forcing the user to open the calendar app. Caps at 20 rows —
        # past that the card stops being scannable and the user should
        # bounce to /calendar.
        from backend.ui_tools import _append
        _append({
            "type":   "events_found",
            "events": events[:20],
            "total":  len(events),
            "window": {"start_iso": start.isoformat(), "end_iso": end.isoformat()},
        })
        out["_llm_hint"] = (
            f"shown_to_user:{len(events)} event(s) in {start.date().isoformat()}"
            f"..{end.date().isoformat()}, rendered as cards. Reply ONE short "
            f"sentence with the count + 'siehe unten' (or equivalent in user's "
            f"language). Optionally call show_calendar with the event ids to "
            f"also open the full view. Do NOT list titles or dates in text — "
            f"the cards carry them."
        )
    return out


def _query_events(start: datetime, end: datetime,
                  title_needle: Optional[str] = None,
                  *,
                  user_id: Optional[int] = None,
                  role: Optional[str] = None,
                  ) -> list[dict[str, Any]]:
    """Pull the events table for a window, normalised to the shape the
    skill returns. Optional case-insensitive substring filter on title.

    Phase C T13 audit fix: workspace scoping. Without this, a workspace
    admin in WS2 chatting "what's on my calendar this month" would see
    every workspace's events (the chat path bypasses /api/events which
    is correctly scoped). `user_id`+`role` come from the SkillContext.
    """
    from backend.database import get_conn
    # Build the WHERE clauses + params first so the visibility filter
    # composes cleanly.
    where: list[str] = ["starts_at >= ?", "starts_at <= ?"]
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if title_needle:
        where.append("lower(title) LIKE ?")
        params.append(f"%{title_needle}%")

    if role != "platform_admin" and user_id is not None:
        from backend import calendars as _cal
        vis_clause, vis_params = _cal.visible_event_filter(user_id, role)
        if vis_clause:
            where.append(vis_clause)
            params.extend(vis_params)

    sql = ("SELECT id, title, starts_at, ends_at, all_day, person "
           "FROM events WHERE " + " AND ".join(where) +
           " ORDER BY starts_at ASC")
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    events = []
    for r in rows:
        starts = r["starts_at"] or ""
        events.append({
            "id":        r["id"],
            "title":     r["title"],
            "starts_at": starts,
            "ends_at":   r["ends_at"],
            "all_day":   bool(r["all_day"]),
            "person":    r["person"],
            "date":      starts[:10] if starts else "",
            "weekday":   _WEEKDAY_NAMES[datetime.fromisoformat(starts).weekday()] if starts else "",
            "time":      "all day" if r["all_day"] else starts[11:16] if len(starts) >= 16 else "",
            "who":       f" ({r['person']})" if r["person"] and r["person"] != "all" else "",
        })
    return events


_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").rstrip("Z"))
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _compute_free_slots(events, window_start, window_end):
    """Walk each day in the window. Subtract event spans from working
    hours, emit any gap >= MIN_SLOT_MIN. Skips all-day events from the
    busy-set but flags the day as "blocked" if there's one."""
    by_day: dict[str, list[tuple[datetime, datetime]]] = {}
    blocked_days: set[str] = set()
    for e in events:
        if e["all_day"]:
            blocked_days.add(e["date"])
            continue
        try:
            s = datetime.fromisoformat(e["starts_at"])
            t = datetime.fromisoformat(e["ends_at"]) if e["ends_at"] else s + timedelta(minutes=30)
        except (ValueError, TypeError):
            continue
        by_day.setdefault(e["date"], []).append((s, t))

    out = []
    cursor = window_start.date()
    end_date = window_end.date()
    while cursor <= end_date:
        date_str = cursor.strftime("%Y-%m-%d")
        if date_str in blocked_days:
            cursor += timedelta(days=1)
            continue
        day_start = datetime.combine(cursor, WORK_START)
        day_end = datetime.combine(cursor, WORK_END)
        # Clip to window edges.
        if cursor == window_start.date() and window_start > day_start:
            day_start = window_start
        if cursor == window_end.date() and window_end < day_end:
            day_end = window_end
        busy = sorted(by_day.get(date_str, []))
        # Merge overlapping busy spans.
        merged: list[tuple[datetime, datetime]] = []
        for s, t in busy:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], t))
            else:
                merged.append((s, t))
        pointer = day_start
        for s, t in merged:
            if s > pointer:
                gap_min = int((min(s, day_end) - pointer).total_seconds() // 60)
                if gap_min >= MIN_SLOT_MIN:
                    out.append({
                        "date": date_str,
                        "start": pointer.strftime("%H:%M"),
                        "end": min(s, day_end).strftime("%H:%M"),
                        "duration_min": gap_min,
                    })
            pointer = max(pointer, t)
        if pointer < day_end:
            gap_min = int((day_end - pointer).total_seconds() // 60)
            if gap_min >= MIN_SLOT_MIN:
                out.append({
                    "date": date_str,
                    "start": pointer.strftime("%H:%M"),
                    "end": day_end.strftime("%H:%M"),
                    "duration_min": gap_min,
                })
        cursor += timedelta(days=1)
    return out
