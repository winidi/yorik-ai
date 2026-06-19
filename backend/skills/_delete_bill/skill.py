"""delete_bill skill — single-row DELETE, apply-then-confirm."""
from __future__ import annotations
from typing import Any


async def execute(ctx, bill_id: int) -> dict[str, Any]:
    if not isinstance(bill_id, int):
        raise ValueError(f"bill_id must be an integer, got {type(bill_id).__name__}")
    if bill_id <= 0:
        raise ValueError(f"bill_id must be positive, got {bill_id}")

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

    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, amount, currency, due_date, recurring, paid, notes "
            "FROM bills WHERE id=?", (bill_id,)).fetchone()
    if not row:
        raise ValueError(f"bill {bill_id} not found (already deleted?)")
    bill_dict = dict(row)

    with get_conn() as conn:
        cur = conn.execute("DELETE FROM bills WHERE id=?", (bill_id,))
        if cur.rowcount != 1:
            raise RuntimeError(f"expected to delete 1 row, deleted {cur.rowcount}")
        conn.commit()

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "bills",
             "reason": f"deleted bill: {bill_dict.get('name')}"})

    from backend import pending_actions as pa
    pa.confirm_then_apply(
        skill="delete_bill",
        ctx=ctx,
        rollback_kind="restore_bill",
        rollback_args={"bill_row": bill_dict},
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

    return {"deleted_bill_id": bill_id, "bill": bill_dict}
