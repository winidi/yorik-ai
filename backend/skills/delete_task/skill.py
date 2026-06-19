"""delete_task skill — single-row DELETE, apply-then-confirm."""
from __future__ import annotations
from typing import Any


async def execute(ctx, task_id: int) -> dict[str, Any]:
    if not isinstance(task_id, int):
        raise ValueError(f"task_id must be an integer, got {type(task_id).__name__}")
    if task_id <= 0:
        raise ValueError(f"task_id must be positive, got {task_id}")

    # Bulk-delete guardrail — same rationale as delete_calendar_event.
    from backend.ask import _deletes_this_turn, DELETE_TURN_LIMIT
    n_so_far = _deletes_this_turn.get()
    if n_so_far >= DELETE_TURN_LIMIT:
        raise ValueError(
            "REFUSED: another item was already deleted in this turn. "
            "To prevent accidental bulk-deletion, only ONE row may be "
            "deleted per request. STOP, list the remaining tasks to the "
            "user with their ids and titles, and wait for the user to "
            "confirm which one(s) to delete next."
        )
    _deletes_this_turn.set(n_so_far + 1)

    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, due_date, done, person, notes, "
            "       category, created_by_user_id "
            "FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise ValueError(f"task {task_id} not found (already deleted?)")
    task_dict = dict(row)

    # Ownership gate: members can only delete tasks they created.
    # Same pattern as delete_calendar_event.
    from backend.calendars import require_row_owner_or_admin
    require_row_owner_or_admin(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        task_dict,
        subject="task",
        owner_col="created_by_user_id",
    )

    with get_conn() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        if cur.rowcount != 1:
            raise RuntimeError(f"expected to delete 1 row, deleted {cur.rowcount}")
        conn.commit()

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "tasks",
             "reason": f"deleted task: {task_dict.get('title')}"})

    from backend import pending_actions as pa
    pa.confirm_then_apply(
        skill="delete_task",
        ctx=ctx,
        rollback_kind="restore_task",
        rollback_args={"task_row": task_dict},
        preview={
            "action":   "delete",
            "task_id":  task_id,
            "task":     {
                "title":    task_dict.get("title"),
                "due_date": task_dict.get("due_date"),
                "done":     bool(task_dict.get("done")),
                "person":   task_dict.get("person"),
                "notes":    task_dict.get("notes"),
                "category": task_dict.get("category"),
            },
        },
    )

    return {"deleted_task_id": task_id, "task": task_dict}
