"""date_info — weekdays and date arithmetic done by code.

The chat test (2026-09-26) had the model write "Der 2. Oktober ist ein
Donnerstag" (a Friday) and plan with the wrong day.
"""

from __future__ import annotations

import calendar
from datetime import date as _date, timedelta
from typing import Any, Dict, Optional

WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _parse(v: Optional[str]) -> _date:
    if not v or str(v).strip().lower() in ("today", "heute"):
        from datetime import datetime
        from backend.push import _tz
        return datetime.now(_tz()).date()
    return _date.fromisoformat(str(v).strip()[:10])


def _add_months(d: _date, n: int) -> _date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def describe(d: _date) -> Dict[str, Any]:
    return {"date": d.isoformat(), "weekday": WEEKDAYS_DE[d.weekday()], "weekday_en": d.strftime("%A"),
            "iso_week": d.isocalendar()[1], "shown": f"{WEEKDAYS_DE[d.weekday()]}, {d.strftime('%d.%m.%Y')}"}


async def execute(ctx, date: Optional[str] = None, add_days: int = 0, add_weeks: int = 0,
                  add_months: int = 0, until: Optional[str] = None, date_str: Optional[str] = None) -> Dict[str, Any]:
    date = date or date_str
    try:
        start = _parse(date)
    except ValueError as exc:
        raise ValueError(f"date={date!r} is not YYYY-MM-DD") from exc
    result = _add_months(start, int(add_months or 0)) + timedelta(days=int(add_days or 0), weeks=int(add_weeks or 0))
    out: Dict[str, Any] = {"start": describe(start), "result": describe(result)}
    hint = f"{out['result']['shown']}"
    if until:
        end = _parse(until)
        out["until"] = describe(end)
        out["days_between"] = (end - start).days
        hint += f"; from {out['start']['shown']} to {out['until']['shown']} are {out['days_between']} days"
    out["_llm_hint"] = hint + ". Use exactly these days and dates."
    return out
