"""delete_contact skill — hard delete; gated by the per-turn delete throttle."""
from __future__ import annotations
from typing import Any


async def execute(ctx, contact_id: int) -> dict[str, Any]:
    if not isinstance(contact_id, int) or contact_id <= 0:
        raise ValueError("contact_id must be a positive integer")

    # Per-turn destructive throttle — same pattern as delete_calendar_event,
    # delete_task, delete_bill. Prevents one ambiguous "delete X" from
    # cascading into multiple deletes in a single LLM turn.
    from backend.ask import _deletes_this_turn, DELETE_TURN_LIMIT
    n_so_far = _deletes_this_turn.get()
    if n_so_far >= DELETE_TURN_LIMIT:
        raise ValueError(
            "REFUSED: another item was already deleted in this turn. "
            "To prevent accidental bulk-deletion, only ONE row may be "
            "deleted per request. STOP, list the remaining contacts to "
            "the user with their ids and names, and wait for explicit "
            "confirmation of which one(s) to delete next."
        )
    _deletes_this_turn.set(n_so_far + 1)

    from backend import contacts as C
    # Ownership gate — same pattern as delete_calendar_event / delete_task.
    # A member can only delete contacts they themselves created.
    pre = C.get(contact_id)
    if not pre:
        raise ValueError(f"contact {contact_id} not found")
    from backend.calendars import require_contact_access
    require_contact_access(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        pre,
    )
    snapshot = C.delete(contact_id)  # raises ValueError if not found

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "contacts",
             "reason": f"deleted contact: {snapshot['display_name']}"})

    from backend import pending_actions as pa
    pa.confirm_then_apply(
        skill="delete_contact",
        ctx=ctx,
        rollback_kind="restore_contact",
        rollback_args={"snapshot": snapshot},
        preview={
            "action":       "delete",
            "contact_id":   contact_id,
            "display_name": snapshot.get("display_name"),
            "channels":     len(snapshot.get("channels") or []),
            "addresses":    len(snapshot.get("addresses") or []),
        },
    )

    return {"deleted_contact_id": contact_id, "contact": snapshot}
