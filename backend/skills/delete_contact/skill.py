"""delete_contact skill — stage ONE contact for deletion (confirm-before-apply).

Nothing is deleted when this returns; contacts.delete() runs when the
user taps "Delete" on the pending_confirmation card.
"""
from __future__ import annotations
from typing import Any


async def execute(ctx, contact_id: int) -> dict[str, Any]:
    if not isinstance(contact_id, int) or contact_id <= 0:
        raise ValueError("contact_id must be a positive integer")

    # Per-turn destructive throttle — one staged delete per request.
    from backend.ask import _deletes_this_turn, DELETE_TURN_LIMIT
    n_so_far = _deletes_this_turn.get()
    if n_so_far >= DELETE_TURN_LIMIT:
        raise ValueError(
            "REFUSED: a delete was already staged in this turn. "
            "To prevent accidental bulk-deletion, only ONE row may be "
            "deleted per request. STOP, list the remaining contacts to "
            "the user with their ids and names, and wait for explicit "
            "confirmation of which one(s) to delete next."
        )
    _deletes_this_turn.set(n_so_far + 1)

    from backend import contacts as C
    pre = C.get(contact_id)
    if not pre:
        raise ValueError(f"contact {contact_id} not found")
    # Ownership gate — a member can only delete contacts they may edit.
    from backend.calendars import require_contact_access
    require_contact_access(
        getattr(ctx, "role", None),
        getattr(ctx, "user_id", None),
        pre,
    )

    from backend import pending_actions as pa
    pending_id = pa.stage_before_apply(
        skill="delete_contact",
        ctx=ctx,
        apply_kind="delete_contact",
        apply_args={"contact_id": contact_id},
        params={"contact_id": contact_id},
        preview={
            "action":       "delete",
            "contact_id":   contact_id,
            "display_name": pre.get("display_name"),
            "channels":     len(pre.get("channels") or []),
            "addresses":    len(pre.get("addresses") or []),
        },
    )

    name = pre.get("display_name") or f"contact {contact_id}"
    return {
        "pending":    True,
        "pending_id": pending_id,
        "contact":    {"id": contact_id, "display_name": pre.get("display_name")},
        "_llm_hint": (
            f"shown_to_user: a confirmation card for deleting '{name}' is on screen. "
            "NOTHING is deleted yet — it happens only when the user taps Delete on the card. "
            "Tell the user the card is waiting for their confirmation; do not claim the contact is deleted."
        ),
    }
