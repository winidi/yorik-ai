"""Recurring-event expansion, shared by the calendar API (main.py
/api/events) and the chat's check_calendar skill.

Moved out of main.py on 2026-09-26: the chat test found that
check_calendar never expanded series, so "wann hat Beate Basketball?"
missed a weekly training the calendar grid showed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


# ── Recurring-event expansion ────────────────────────────────────────
# `events.recurring` stores a short code; we materialise virtual
# instances inside the visible window so the grid renders them.
#
# Supported codes:
#   daily          — every day
#   weekly         — every 7 days, same weekday as starts_at
#   weekdays       — shorthand for {Mon, Tue, Wed, Thu, Fri}
#   weekdays:1,3,5 — explicit ISO weekday set (1=Mon ... 7=Sun)
#   monthly        — same day-of-month each month (clamped to last day)
#   yearly         — same month/day each year (Feb 29 → Feb 28 on non-leap)
#
# v1: editing a recurring event edits the whole series. Per-instance
# skips/moves would need an `event_exceptions` side-table.

def _parse_weekdays_spec(rec: str) -> Optional[set]:
    if rec == "weekdays":
        return {1, 2, 3, 4, 5}
    if rec.startswith("weekdays:"):
        s: set = set()
        for p in rec.split(":", 1)[1].split(","):
            try:
                n = int(p.strip())
            except ValueError:
                continue
            if 1 <= n <= 7:
                s.add(n)
        return s or None
    return None


def _add_months(dt: datetime, n: int) -> datetime:
    import calendar as _cal
    m = dt.month - 1 + n
    y = dt.year + m // 12
    m = m % 12 + 1
    d = min(dt.day, _cal.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)


def _expand_recurring(
    rows: List[Dict[str, Any]],
    window_start_iso: str,
    window_end_iso: str,
) -> List[Dict[str, Any]]:
    """Materialise virtual occurrences in [window_start, window_end).

    The original-date occurrence is NOT emitted — combine with the base
    SELECT result. Each instance carries `occurrence_date` so React can
    build stable keys like `<id>_<occurrence_date>`.
    """
    from datetime import timedelta as _td
    try:
        ws = datetime.strptime(window_start_iso[:10], "%Y-%m-%d")
        we = datetime.strptime(window_end_iso[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        rec = (r.get("recurring") or "").strip().lower()
        if not rec:
            continue
        starts_at = r.get("starts_at") or ""
        if len(starts_at) < 10:
            continue
        try:
            base_start = datetime.strptime(starts_at[:10], "%Y-%m-%d")
        except ValueError:
            continue
        start_tail = starts_at[10:] or "T00:00:00"
        ends_at = r.get("ends_at") or ""
        end_tail = ends_at[10:] if len(ends_at) >= 10 else ""
        end_day_offset = 0
        if len(ends_at) >= 10:
            try:
                end_day_offset = (
                    datetime.strptime(ends_at[:10], "%Y-%m-%d") - base_start
                ).days
            except ValueError:
                end_day_offset = 0

        wd_set = _parse_weekdays_spec(rec)
        dates: List[datetime] = []
        if rec == "daily":
            d = max(base_start + _td(days=1), ws)
            while d < we:
                dates.append(d)
                d += _td(days=1)
        elif rec == "weekly":
            d = base_start + _td(days=7)
            if d < ws:
                gap = (ws - d).days
                d += _td(days=((gap + 6) // 7) * 7)
            while d < we:
                dates.append(d)
                d += _td(days=7)
        elif wd_set is not None:
            d = max(base_start + _td(days=1), ws)
            while d < we:
                if d.isoweekday() in wd_set:
                    dates.append(d)
                d += _td(days=1)
        elif rec == "monthly":
            # Always derive from base_start so a Jan-31 series stays
            # on the 31st (clamped to the last day in shorter months)
            # instead of drifting down to 28 after February.
            n = 1
            while True:
                d = _add_months(base_start, n)
                if d >= we:
                    break
                if d >= ws:
                    dates.append(d)
                n += 1
        elif rec == "yearly":
            n = 1
            while True:
                try:
                    d = base_start.replace(year=base_start.year + n)
                except ValueError:
                    d = base_start.replace(year=base_start.year + n, day=28)
                if d >= we:
                    break
                if d >= ws:
                    dates.append(d)
                n += 1
        else:
            continue

        for od in dates:
            inst = dict(r)
            iso = od.strftime("%Y-%m-%d")
            inst["starts_at"] = iso + start_tail
            if end_tail:
                end_d = od + _td(days=end_day_offset)
                inst["ends_at"] = end_d.strftime("%Y-%m-%d") + end_tail
            inst["occurrence_date"] = iso
            inst["is_recurring_instance"] = True
            out.append(inst)
    return out
