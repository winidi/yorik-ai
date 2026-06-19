"""Recurrence shorthand → next due-date.

The `tasks.recurrence_rule` column stores free-form text the user
(or the LLM during natural-language quick-capture) typed. We accept
a small, opinionated grammar instead of full RFC 5545 RRULE — most
household tasks fit one of:

    daily                 → +1 day
    weekly                → +7 days
    every 2 weeks         → +14 days   (N weeks supported)
    every 3 days          → +N days
    monthly               → +1 month (same day-of-month if valid,
                                       otherwise last-of-month)
    yearly                → +1 year
    every Mon             → next Monday on/after today+1
    every Mon,Wed,Fri     → next listed weekday on/after today+1

Returns ``None`` when the rule can't be parsed — the caller should
treat that as "no recurrence" and not materialise a follow-up. Better
to lose a recurrence than to spam the user with bogus repeats from a
broken rule string.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional


_WEEKDAY_MAP = {
    "mon": 0, "monday": 0, "montag": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "dienstag": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "mittwoch": 2,
    "thu": 3, "thurs": 3, "thursday": 3, "donnerstag": 3,
    "fri": 4, "friday": 4, "freitag": 4,
    "sat": 5, "saturday": 5, "samstag": 5, "sonnabend": 5,
    "sun": 6, "sunday": 6, "sonntag": 6,
}


def next_due_date(rule: str, from_date: Optional[date] = None) -> Optional[date]:
    """Compute the next due date AFTER ``from_date`` (default: today).

    Returns ``None`` for unparseable rules. Always returns a date
    strictly AFTER from_date — never the same day — so a task done on
    its due date moves forward, not into a refresh loop.
    """
    if not rule or not isinstance(rule, str):
        return None
    s = rule.strip().lower()
    if not s:
        return None
    base = from_date or date.today()

    # Day shorthands
    if s in ("daily", "every day", "täglich"):
        return base + timedelta(days=1)
    if s in ("weekly", "every week", "wöchentlich"):
        return base + timedelta(days=7)
    if s in ("biweekly", "fortnightly", "every 2 weeks", "every other week"):
        return base + timedelta(days=14)
    if s in ("monthly", "every month", "monatlich"):
        return _add_months(base, 1)
    if s in ("quarterly", "every quarter", "every 3 months"):
        return _add_months(base, 3)
    if s in ("yearly", "annually", "every year", "jährlich"):
        return _add_months(base, 12)

    # "every N days/weeks/months"
    m = re.fullmatch(r"every\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("day"):
            return base + timedelta(days=n)
        if unit.startswith("week"):
            return base + timedelta(days=7 * n)
        if unit.startswith("month"):
            return _add_months(base, n)
        if unit.startswith("year"):
            return _add_months(base, 12 * n)

    # "every Mon" / "every mon,wed,fri"
    if s.startswith("every "):
        rest = s[len("every "):]
        # Split on commas + spaces + "und"/"and"
        parts = re.split(r"[,/\s]+|und|and", rest)
        wanted: set[int] = set()
        for p in parts:
            p = p.strip()
            if not p:
                continue
            idx = _WEEKDAY_MAP.get(p)
            if idx is None:
                # Unknown token — bail rather than guess
                return None
            wanted.add(idx)
        if wanted:
            for offset in range(1, 15):  # search up to 2 weeks ahead
                d = base + timedelta(days=offset)
                if d.weekday() in wanted:
                    return d
            return None

    return None


def _add_months(d: date, months: int) -> date:
    """Add `months` months. If the target month has no matching day-of-
    month (e.g. Jan 31 + 1 month), clamp to the last day of that month
    instead of overflowing into the next."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # Clamp day to month length
    if m == 12:
        next_month_first = date(y + 1, 1, 1)
    else:
        next_month_first = date(y, m + 1, 1)
    last_day = (next_month_first - timedelta(days=1)).day
    return date(y, m, min(d.day, last_day))


def materialise_next_instance(
    *, conn, task_id: int,
) -> Optional[int]:
    """Insert the next instance of a recurring task. Returns the new
    task's id, or None when:
      - The task has no recurrence_rule
      - The rule can't be parsed
      - A child for this parent already exists with done=0 (don't
        stack duplicates if the user marked done twice in a row)

    Called from PATCH /api/tasks/{id} and from the update_task skill
    on the 0→1 done transition. Inserts a sibling row (NOT a subtask)
    that carries the same fields + recurrence_rule + a fresh due_date.
    """
    row = conn.execute(
        "SELECT title, due_date, person, category, notes, space_id, "
        "       priority, estimated_minutes, created_by_user_id, "
        "       recurrence_rule, parent_task_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    rule = row["recurrence_rule"]
    if not rule:
        return None

    # Base the next date on the PREVIOUS due_date if it's set
    # (preserves cadence — "every Mon" stays on Monday even if you
    # marked it done late). Fall back to today otherwise.
    from datetime import date as _date
    base = None
    if row["due_date"]:
        try:
            base = _date.fromisoformat(row["due_date"][:10])
        except ValueError:
            base = None
    nxt = next_due_date(rule, base or _date.today())
    if nxt is None:
        return None

    # Idempotency — if there's already an open task with the SAME
    # title + recurrence_rule (and not done), don't stack another one.
    existing = conn.execute(
        "SELECT id FROM tasks "
        "WHERE title = ? AND recurrence_rule = ? AND done = 0 AND id != ?",
        (row["title"], rule, task_id),
    ).fetchone()
    if existing:
        return None

    cur = conn.execute(
        "INSERT INTO tasks (title, due_date, done, person, category, notes, "
        " space_id, priority, estimated_minutes, created_by_user_id, "
        " recurrence_rule, parent_task_id) "
        "VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["title"], nxt.isoformat(),
            row["person"], row["category"], row["notes"],
            row["space_id"], row["priority"], row["estimated_minutes"],
            row["created_by_user_id"], rule, row["parent_task_id"],
        ),
    )
    return int(cur.lastrowid)
