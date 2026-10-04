"""delete_bill skill — stage ONE bill for deletion (confirm-before-apply).

Nothing is deleted when this returns; the DELETE runs when the user taps
"Delete" on the pending_confirmation card (pending_actions.apply), the
same as tasks, appointments and contacts. "Keep" discards it.
"""
from __future__ import annotations
from typing import Any


async def execute(ctx, bill_id: int) -> dict[str, Any]:
    if not isinstance(bill_id, int):
        raise ValueError(f"bill_id must be an integer, got {type(bill_id).__name__}")
    if bill_id <= 0:
        raise ValueError(f"bill_id must be positive, got {bill_id}")

    from backend import bills, spaces
    row = bills.get_bill(bill_id)
    if not row or not spaces.can_write_row(getattr(ctx, "user_id", None), getattr(ctx, "role", None),
                                           "bills", row):
        raise ValueError(f"bill {bill_id} not found (already deleted?)")
    bill_dict = {k: v for k, v in row.items() if k not in ("overdue", "days_left", "link")}

    # Bulk-delete guardrail — same rationale as delete_calendar_event.
    from backend.ask import _deletes_this_turn, DELETE_TURN_LIMIT
    n_so_far = _deletes_this_turn.get()
    if n_so_far >= DELETE_TURN_LIMIT:
        raise ValueError(
            "REFUSED: another item was already deleted in this turn. "
            "To prevent accidental bulk-deletion, only ONE row may be "
            "deleted per request. STOP, list the remaining bills to the "
            "user with their ids and names, and wait for the user to "
            "confirm which one(s) to delete next."
        )
    _deletes_this_turn.set(n_so_far + 1)

    from backend import pending_actions as pa
    pending_id = pa.stage_before_apply(
        skill="delete_bill",
        ctx=ctx,
        apply_kind="delete_bill",
        apply_args={"bill_id": bill_id, "name": bill_dict.get("name")},
        params={"bill_id": bill_id},
        preview={
            "action":   "delete",
            "bill_id":  bill_id,
            "bill":     {
                "name":     bill_dict.get("name"),
                "amount":   bill_dict.get("amount"),
                "currency": bill_dict.get("currency"),
                "due_date": bill_dict.get("due_date"),
                "paid":     bool(bill_dict.get("paid")),
            },
        },
    )

    name = bill_dict.get("name") or f"bill {bill_id}"
    return {
        "pending":    True,
        "pending_id": pending_id,
        "bill":       bill_dict,
        "_llm_hint": (
            f"shown_to_user: a confirmation card for deleting the bill '{name}' is on screen. "
            "NOTHING is deleted yet — it happens only when the user taps Delete on the card. "
            "Tell the user the card is waiting for their confirmation; do not claim the bill is deleted."
        ),
    }
