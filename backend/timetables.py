"""Children's school timetables (family board, migration 147).

One JSON document per child in `timetables`:
{"periods": [{"start": "08:00", "end": "08:45"}, ...],
 "cells": {"<weekday 0-4>-<period index>": {"subject": "...", "room": "..."}}}

The board routes in main.py store and show them; this module is the
shared part, so the chat and agent skills can answer "what lesson does
Ben have at ten on Tuesday" from the same data.

Who may read a child's timetable: the child itself, and a household
member who is not a restricted account (a parent) — the same people who
may change it on the board.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

DEFAULT_PERIODS = [("08:00", "08:45"), ("08:50", "09:35"), ("09:55", "10:40"),
                   ("10:45", "11:30"), ("11:45", "12:30"), ("12:35", "13:20")]

_PARENT_ROLES = ("member", "admin", "platform_admin")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MAX_DAYS = 14


def clean(raw: Any) -> Dict[str, Any]:
    """Whatever comes in, what is stored is a small, well-formed plan:
    up to 12 periods with HH:MM times, cells for Mon–Fri only."""
    raw = raw if isinstance(raw, dict) else {}
    periods = []
    for p in (raw.get("periods") or [])[:12]:
        p = p if isinstance(p, dict) else {}
        start, end = str(p.get("start") or ""), str(p.get("end") or "")
        periods.append({"start": start if _HHMM.match(start) else "", "end": end if _HHMM.match(end) else ""})
    cells: Dict[str, Dict[str, str]] = {}
    for key, c in (raw.get("cells") or {}).items() if isinstance(raw.get("cells"), dict) else []:
        m = re.fullmatch(r"([0-4])-(\d{1,2})", str(key))
        if not m or int(m.group(2)) >= len(periods) or not isinstance(c, dict):
            continue
        subject = str(c.get("subject") or "").strip()[:40]
        room = str(c.get("room") or "").strip()[:20]
        if subject or room:
            cells[f"{m.group(1)}-{int(m.group(2))}"] = {"subject": subject, "room": room}
    return {"periods": periods, "cells": cells}


def readable_children(user_id: Optional[str], role: Optional[str]) -> Dict[str, str]:
    """Children (restricted household accounts) whose timetable this
    person may read → {user id: first name}. A child gets itself, a
    parent every child in the household, anyone else nobody."""
    if user_id is None:
        return {}
    from .database import conn_ctx
    r = (role or "").lower()
    with conn_ctx() as c:
        if r == "restricted":
            rows = c.execute(
                "SELECT id, first_name, name FROM user_profiles WHERE id = ? "
                "AND lower(role) = 'restricted' AND (disabled = 0 OR disabled IS NULL)",
                (str(user_id),)).fetchall()
        elif r in _PARENT_ROLES:
            in_household = c.execute(
                "SELECT 1 FROM space_members m JOIN spaces s ON s.id = m.space_id "
                "WHERE s.slug = 'household' AND m.user_id = ?", (str(user_id),)).fetchone()
            if not in_household:
                return {}
            rows = c.execute(
                "SELECT u.id, u.first_name, u.name FROM user_profiles u "
                "JOIN space_members m ON m.user_id = u.id "
                "JOIN spaces s ON s.id = m.space_id AND s.slug = 'household' "
                "WHERE lower(u.role) = 'restricted' AND (u.disabled = 0 OR u.disabled IS NULL) "
                "ORDER BY u.created_at, u.id").fetchall()
        else:
            return {}
    return {str(row["id"]): (row["first_name"] or (row["name"] or "").split(" ")[0] or "") for row in rows}


def plans_for(user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Stored, filled timetables of these children → {user id: plan}."""
    if not user_ids:
        return {}
    from .database import conn_ctx
    ph = ",".join("?" * len(user_ids))
    with conn_ctx() as c:
        rows = c.execute(f"SELECT user_id, data FROM timetables WHERE user_id IN ({ph})",
                         list(user_ids)).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        try:
            plan = clean(json.loads(row["data"]))
        except (ValueError, TypeError):
            continue
        if plan["cells"]:
            out[str(row["user_id"])] = plan
    return out


def lessons_between(plans: Dict[str, Dict[str, Any]], names: Dict[str, str],
                    start: datetime, end: datetime, *,
                    subject_contains: Optional[str] = None) -> List[Dict[str, Any]]:
    """The weekly plans laid out on the dates of the window (at most
    MAX_DAYS days): every lesson that overlaps [start, end], oldest
    first. A period without times counts for its whole day. School
    holidays are unknown to the plan."""
    needle = (subject_contains or "").strip().lower() or None
    out: List[Dict[str, Any]] = []
    day: date = start.date()
    last = min(end.date(), day + timedelta(days=MAX_DAYS - 1))
    while day <= last:
        wd = day.weekday()
        if wd <= 4:
            for uid, plan in plans.items():
                for idx, period in enumerate(plan["periods"]):
                    cell = plan["cells"].get(f"{wd}-{idx}")
                    if not cell:
                        continue
                    if needle and needle not in cell["subject"].lower():
                        continue
                    p_start, p_end = period.get("start") or "", period.get("end") or ""
                    if p_start and p_end:
                        s = datetime.combine(day, datetime.strptime(p_start, "%H:%M").time())
                        e = datetime.combine(day, datetime.strptime(p_end, "%H:%M").time())
                        if not (s < end and e > start):
                            continue
                    out.append({
                        "who": names.get(uid, ""),
                        "date": day.isoformat(),
                        "weekday": _WEEKDAYS[wd],
                        "period": idx + 1,
                        "start": p_start,
                        "end": p_end,
                        "subject": cell["subject"],
                        "room": cell["room"],
                    })
        day += timedelta(days=1)
    out.sort(key=lambda x: (x["date"], x["start"] or "99:99", x["period"], x["who"]))
    return out
