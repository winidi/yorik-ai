"""update_task skill — apply-then-confirm UPDATE."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Optional


async def execute(
    ctx,
    task_id: int,
    title: Optional[str] = None,
    due_date: Optional[str] = None,
    done: Optional[bool] = None,
    person: Optional[str] = None,
    category: Optional[str] = None,
    notes: Optional[str] = None,
    parent_task_id: Optional[int] = None,
    recurrence_rule: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(task_id, int) or task_id <= 0:
        raise ValueError("task_id must be a positive integer")
    if due_date and due_date.strip():
        try:
            datetime.strptime(due_date[:10], "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"due_date must be YYYY-MM-DD: {e}")
        # Tasks have date-granularity dues; trim any time component the
        # LLM appended (e.g. "2026-06-03T17:00:00") so the frontend's
        # formatDue (which does due + "T00:00:00") doesn't choke.
        due_date = due_date[:10]

    # Empty-string recurrence_rule = explicit clear → store NULL.
    if isinstance(recurrence_rule, str) and not recurrence_rule.strip():
        recurrence_rule = None

    updates: dict[str, Any] = {}
    for k, v in (
        ("title",           title.strip() if isinstance(title, str) else title),
        ("due_date",        None if (isinstance(due_date, str) and due_date == "") else due_date),
        ("done",            None if done is None else (1 if done else 0)),
        ("person",          person),
        ("category",        category),
        ("notes",           notes),
        ("parent_task_id",  parent_task_id),
        ("recurrence_rule", recurrence_rule.strip().lower() if isinstance(recurrence_rule, str) else recurrence_rule),
    ):
        if v is not None:
            updates[k] = v
    if not updates:
        raise ValueError("nothing to update — pass at least one field")

    from backend.database import get_conn
    with get_conn() as conn:
        before = conn.execute(
            "SELECT id, title, due_date, done, person, notes, space_id, "
            "       category, parent_task_id, recurrence_rule, "
            "       created_by_user_id "
            "FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not before:
        raise ValueError(f"task {task_id} not found")
    before_dict = dict(before)

    # Ownership gate: members can only edit tasks they created. Admin
    # bypasses. Same pattern as the calendar event skills (Phase 5).
    from backend.calendars import require_row_owner_or_admin
    require_row_owner_or_admin(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        before_dict,
        subject="task",
        owner_col="created_by_user_id",
    )

    set_clause = ", ".join(f"{k}=?" for k in updates)
    params = list(updates.values()) + [task_id]
    materialised_new_id: Optional[int] = None
    with get_conn() as conn:
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", params)
        # Recurring task: detect 0→1 done flip and spawn the next
        # instance in the same transaction so the rollback can sweep
        # the child too. Best-effort — never block the actual update.
        if "done" in updates and updates["done"] == 1 and int(before_dict.get("done") or 0) == 0:
            try:
                from backend import tasks_recurrence as _rec
                materialised_new_id = _rec.materialise_next_instance(conn=conn, task_id=task_id)
            except Exception:  # noqa: BLE001
                materialised_new_id = None
        row = conn.execute(
            "SELECT id, title, due_date, done, person, notes, space_id, "
            "       category, parent_task_id, recurrence_rule "
            "FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.commit()

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "tasks", "highlight_id": task_id,
             "reason": f"updated task: {row['title']}"})

    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        before_for_rollback = {k: before_dict[k] for k in updates}
        pa.stage_with_rollback(
            skill="update_task",
            rollback_kind="revert_task_fields",
            rollback_args={"task_id": task_id, "before": before_for_rollback},
            preview={
                "action":   "update",
                "task_id":  task_id,
                "before":   {k: before_dict[k] for k in (
                    "title", "due_date", "done", "person", "notes", "category"
                )},
                "after":    {k: (updates.get(k) if k in updates else before_dict[k]) for k in (
                    "title", "due_date", "done", "person", "notes", "category"
                )},
                "materialised_recurrence_id": materialised_new_id,
            },
            ctx=ctx,
        )

    out: dict[str, Any] = {"task_id": task_id, "task": dict(row) if row else None}
    if materialised_new_id is not None:
        out["next_instance_id"] = materialised_new_id
        out["_llm_hint"] = (
            f"Recurring task — the next instance was auto-created "
            f"(id={materialised_new_id}). Mention it to the user briefly."
        )
    return out
