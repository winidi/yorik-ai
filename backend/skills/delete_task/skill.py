"""delete_task skill — stage ONE task for deletion (confirm-before-apply).

Nothing is deleted when this returns; the DELETE runs when the user taps
"Delete" on the pending_confirmation card (pending_actions.apply).
"""
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
            "REFUSED: a delete was already staged in this turn. "
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
    from backend.calendars import require_row_owner_or_admin
    require_row_owner_or_admin(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        task_dict,
        subject="task",
        owner_col="created_by_user_id",
    )

    from backend import pending_actions as pa
    pending_id = pa.stage_before_apply(
        skill="delete_task",
        ctx=ctx,
        apply_kind="delete_task",
        apply_args={"task_id": task_id, "title": task_dict.get("title")},
        params={"task_id": task_id},
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

    title = task_dict.get("title") or f"task {task_id}"
    return {
        "pending":    True,
        "pending_id": pending_id,
        "task":       task_dict,
        "_llm_hint": (
            f"shown_to_user: a confirmation card for deleting '{title}' is on screen. "
            "NOTHING is deleted yet — it happens only when the user taps Delete on the card. "
            "Tell the user the card is waiting for their confirmation; do not claim the task is deleted."
        ),
    }
