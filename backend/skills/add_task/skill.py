"""add_task skill — apply-then-confirm INSERT on tasks."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Optional


async def execute(
    ctx,
    title: str,
    due_date: Optional[str] = None,
    person: Optional[str] = None,
    category: Optional[str] = None,
    notes: Optional[str] = None,
    parent_task_id: Optional[int] = None,
    recurrence_rule: Optional[str] = None,
) -> dict[str, Any]:
    if not title or not title.strip():
        raise ValueError("title is required")
    if due_date:
        try:
            datetime.strptime(due_date[:10], "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"due_date must be YYYY-MM-DD: {e}")
        # Tasks have date-granularity dues; trim any time component the
        # LLM appended (e.g. "2026-06-03T17:00:00") so the frontend's
        # formatDue (which does due + "T00:00:00") doesn't choke.
        due_date = due_date[:10]
    # parent_task_id: validate it exists if set.
    if parent_task_id is not None:
        if not isinstance(parent_task_id, int) or parent_task_id <= 0:
            raise ValueError(f"parent_task_id must be a positive integer, got {parent_task_id!r}")
    if recurrence_rule is not None and not isinstance(recurrence_rule, str):
        raise ValueError("recurrence_rule must be a string or null")

    creator_id = getattr(ctx, "user_id", None)
    # Phase B: default to creator's personal space so the task starts
    # private-to-creator. Override via UI / explicit space picker later.
    from backend import spaces as _sp
    space_id = _sp.personal_space_id(creator_id) if creator_id else None
    from backend.database import get_conn
    with get_conn() as conn:
        if parent_task_id is not None:
            exists = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (parent_task_id,),
            ).fetchone()
            if not exists:
                raise ValueError(f"parent_task_id {parent_task_id} not found")
        cur = conn.execute(
            "INSERT INTO tasks (title, due_date, done, person, notes, "
            " category, created_by_user_id, space_id, "
            " parent_task_id, recurrence_rule) "
            "VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
            (title.strip(), due_date, person, notes,
             category, creator_id, space_id, parent_task_id,
             (recurrence_rule or "").strip().lower() or None),
        )
        task_id = cur.lastrowid
        # Whose task is it: the named household member's when there is
        # one, otherwise the creator's (so it shows on their calendar via
        # the assignee link, as the REST POST does). Until 2026-10-01 the
        # creator was always added too — "Müll rausbringen für Ben" sat in
        # Anna's list and in both columns of the family board.
        member = None
        if person and person.strip():
            member = conn.execute(
                "SELECT id FROM user_profiles WHERE lower(name) = lower(?) LIMIT 1", (person.strip(),)
            ).fetchone()
        assignee = member["id"] if member else creator_id
        if assignee is not None:
            conn.execute(
                "INSERT OR IGNORE INTO task_assignees (task_id, user_id) VALUES (?, ?)",
                (task_id, assignee),
            )
        row = conn.execute(
            "SELECT id, title, due_date, done, person, notes, space_id, "
            "       category, parent_task_id, recurrence_rule "
            "FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.commit()

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "tasks", "highlight_id": task_id,
             "reason": f"created task: {title.strip()}"})

    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="add_task",
            rollback_kind="delete_task",
            rollback_args={"task_id": task_id},
            preview={
                "action":          "create",
                "task_id":         task_id,
                "title":           title.strip(),
                "due_date":        due_date,
                "person":          person,
                "category":        category,
                "notes":           notes,
                "parent_task_id":  parent_task_id,
                "recurrence_rule": (recurrence_rule or "").strip().lower() or None,
            },
            ctx=ctx,
        )

    return {"task_id": task_id, "task": dict(row) if row else None}
