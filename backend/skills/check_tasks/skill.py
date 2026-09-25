"""check_tasks skill — date-bounded read of the tasks table.

Mirrors check_calendar's contract so briefing templates can compose
the two interchangeably. No mutation — for that the chat agent has
add/update/delete_task.
"""

from __future__ import annotations

from typing import Any, Optional


async def execute(
    ctx,
    start_iso: Optional[str] = None,
    end_iso: Optional[str] = None,
    include_undated: bool = False,
    include_done: bool = False,
    person: Optional[str] = None,
    include_rows: bool = False,
    overdue_only: bool = False,
    mine_only: bool = True,
    everyone: bool = False,
) -> dict[str, Any]:
    from backend.database import get_conn

    # Bound parsing — accept either YYYY-MM-DD or full ISO datetime.
    # We compare to due_date (DATE column), so trim datetimes to the
    # date prefix when SQLite-comparing.
    start_date = (start_iso or "")[:10] or None
    end_date   = (end_iso   or "")[:10] or None

    where: list[str] = []
    params: list[Any] = []

    if not include_done:
        where.append("done = 0")

    # overdue_only is an explicit shortcut for "everything past due,
    # nothing today or future." Without it, small LLMs misbuild the
    # date window (start_iso=today + end_iso=today returns today-only,
    # not overdue) and then mis-interpret 0 results as "nothing overdue".
    # When set, overrides any caller-supplied date window and ignores
    # include_undated (a task with no due_date can't be overdue).
    if overdue_only:
        where.append("due_date IS NOT NULL AND due_date < date('now')")
    elif start_date and end_date:
        # Date-window predicate — tricky bit. Three cases:
        #   1. window set, include_undated=False → due_date BETWEEN start AND end
        #   2. window set, include_undated=True  → BETWEEN ... OR due_date IS NULL
        #   3. no window                          → no date filter
        if include_undated:
            where.append("(due_date IS NULL OR (due_date >= ? AND due_date <= ?))")
        else:
            where.append("(due_date >= ? AND due_date <= ?)")
        params.extend([start_date, end_date])
    elif start_date:
        if include_undated:
            where.append("(due_date IS NULL OR due_date >= ?)")
        else:
            where.append("due_date >= ?")
        params.append(start_date)
    elif end_date:
        if include_undated:
            where.append("(due_date IS NULL OR due_date <= ?)")
        else:
            where.append("due_date <= ?")
        params.append(end_date)
    # No window → no extra filter (returns all matching open tasks)

    # Phase C T13 audit fix: scope tasks to spaces the caller can see.
    # Without this, a workspace admin in WS2 chatting "show me my tasks"
    # would see every workspace's tasks DB-wide (the SqliteRunner's
    # filter_query_by_role bypass only enforces table-allowlists, not
    # row-level scoping). Mirrors backend/main.py:3366's tasks-list
    # endpoint behaviour.
    #
    # Two questions, asked separately (audit 2026-09-25, A2): who may see
    # a task (row_filter, always) and whose list it is on. By default the
    # person's own list, the Tasks app's rule — "meine Aufgaben" used to
    # come back as the whole household's. `person` asks for someone
    # else's list, `everyone` for all the person may see.
    from backend import spaces as _sp
    user_id = getattr(ctx, "user_id", None)
    role = getattr(ctx, "role", None)
    clause, clause_params = _sp.row_filter(user_id, role, "tasks")
    where.append(clause)
    params.extend(clause_params)
    scope = "mine"
    if person:
        who = _resolve_person(person)
        if who:
            where.append("(id IN (SELECT task_id FROM task_assignees WHERE user_id IN ("
                         + ",".join("?" * len(who)) + ")))")
            params.extend(who)
        else:                          # not a household member: the free-text column
            where.append("(person = ? OR person LIKE ?)")
            params.extend([person, f"%{person}%"])
        scope = f"person:{person}"
    elif everyone or not mine_only:
        scope = "everyone"
    else:
        own, own_params = _sp.own_task_filter(user_id)
        where.append(own)
        params.extend(own_params)

    sql = (
        "SELECT id, title, due_date, done, person, category, priority, "
        "       estimated_minutes, parent_task_id, recurrence_rule, created_by_user_id "
        "FROM tasks"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Dated tasks first (chronological), then undated. Within each
    # bucket, higher priority first.
    sql += " ORDER BY due_date IS NULL, due_date ASC, priority DESC, id ASC LIMIT 50"

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        # Subtask progress — for every parent in the result set, count
        # its open + done children so the LLM can answer "how far along
        # is the Q1 Steuer-task" without a second roundtrip.
        parent_ids = [r["id"] for r in rows if r.get("parent_task_id") is None]
        if parent_ids:
            placeholders = ",".join("?" * len(parent_ids))
            child_rows = conn.execute(
                f"SELECT parent_task_id, "
                f"       SUM(CASE WHEN done=0 THEN 1 ELSE 0 END) AS open_n, "
                f"       SUM(CASE WHEN done=1 THEN 1 ELSE 0 END) AS done_n "
                f"FROM tasks WHERE parent_task_id IN ({placeholders}) "
                f"GROUP BY parent_task_id",
                parent_ids,
            ).fetchall()
            by_parent = {
                r["parent_task_id"]: {
                    "open":  int(r["open_n"] or 0),
                    "done":  int(r["done_n"] or 0),
                }
                for r in child_rows
            }
            for r in rows:
                pid = r["id"]
                if pid in by_parent:
                    r["subtasks"] = by_parent[pid]

    _label_whose(rows, user_id)
    others = sum(1 for r in rows if r.get("person"))

    # Surface the same tasks as an interactive chat card so the LLM's prose
    # answer is mirrored by clickable rows (mark-done + jump-to-/tasks)
    # instead of a static markdown list the user can't act on. Cap at 20
    # — past that the card stops being scannable and the user should
    # bounce to /tasks anyway.
    if rows:
        from backend.ui_tools import _append
        _append({
            "type":   "tasks_found",
            "tasks":  rows[:20],
            "total":  len(rows),
            "window": {"start_iso": start_iso, "end_iso": end_iso},
            "scope":  scope,
        })

    # Briefing callers ask for include_rows=True because their renderer
    # has no access to the chat ui_tools side-channel — they need the
    # actual rows to draw the card. Chat callers leave it False so the
    # small LLM doesn't re-enumerate (and hallucinate) row content
    # that's already visible in the ui_tools card.
    if include_rows:
        return {
            "tasks":  rows,
            "count":  len(rows),
            "window": {"start_iso": start_iso, "end_iso": end_iso},
            "scope":  scope,
        }

    whose = {
        "mine":     "the person's OWN tasks only",
        "everyone": f"everything the person may see; {others} of them belong to "
                    f"someone else (their name is on the card)",
    }.get(scope, f"the tasks of {person}")
    return {
        "_llm_hint": (
            f"shown_to_user:{len(rows)} open task(s) — {whose} — rendered as cards. "
            f"Reply ONE short sentence with the count + 'siehe Karten unten'. "
            f"Do NOT enumerate titles or IDs in your text — you don't have "
            f"the row content here, only the count."
        ),
        "count": len(rows),
        "window": {"start_iso": start_iso, "end_iso": end_iso},
        "scope": scope,
    }


def _resolve_person(name: str) -> list[str]:
    """Household members whose name or first name matches `name`."""
    from backend.database import get_conn
    n = (name or "").strip().lower()
    if not n:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM user_profiles WHERE lower(first_name) = ? OR lower(name) = ? "
            "OR lower(name) LIKE ?", (n, n, f"{n} %")).fetchall()
    return [str(r["id"]) for r in rows]


def _label_whose(rows: list[dict[str, Any]], me: Any) -> None:
    """Put the owner's first name into `person` — the card shows it — for
    every task that isn't the asker's alone; their own stay unlabelled.
    Owner: the assignees, else whoever created it. The old free-text
    `person` column said nothing reliable (audit 2026-09-25, A3)."""
    from backend import spaces as _sp
    from backend.calendars import event_owner_names
    me = str(me) if me is not None else None
    people = _sp.task_people([r["id"] for r in rows])
    creators = event_owner_names([r.get("created_by_user_id") for r in rows])
    for r in rows:
        who = people.get(r["id"]) or []
        if who:
            names = [p["first_name"] for p in who if p["user_id"] != me]
            if names and len(names) < len(who):      # shared with the asker
                names = ["+ " + ", ".join(names)]
        else:
            c = r.get("created_by_user_id")
            names = [creators.get(str(c), "")] if c and str(c) != me else []
        r["person"] = ", ".join(n for n in names if n) or None
        r.pop("created_by_user_id", None)
