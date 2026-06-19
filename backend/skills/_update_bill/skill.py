"""update_bill skill — apply-then-confirm UPDATE."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Optional


async def execute(
    ctx,
    bill_id: int,
    name: Optional[str] = None,
    amount: Optional[float] = None,
    currency: Optional[str] = None,
    due_date: Optional[str] = None,
    recurring: Optional[str] = None,
    paid: Optional[bool] = None,
    notes: Optional[str] = None,
    document_id: Optional[int] = None,
) -> dict[str, Any]:
    if not isinstance(bill_id, int) or bill_id <= 0:
        raise ValueError("bill_id must be a positive integer")
    if amount is not None:
        try:
            amount = float(amount)
        except (TypeError, ValueError) as e:
            raise ValueError(f"amount must be a number: {e}")
    if due_date and due_date.strip():
        try:
            datetime.strptime(due_date[:10], "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"due_date must be YYYY-MM-DD: {e}")

    updates: dict[str, Any] = {}
    for k, v in (
        ("name",        name.strip() if isinstance(name, str) else name),
        ("amount",      amount),
        ("currency",    currency),
        ("due_date",    None if (isinstance(due_date, str) and due_date == "") else due_date),
        ("recurring",   recurring),
        ("paid",        None if paid is None else (1 if paid else 0)),
        ("notes",       notes),
        ("document_id", document_id),
    ):
        if v is not None:
            updates[k] = v
    if not updates:
        raise ValueError("nothing to update — pass at least one field")

    from backend.database import get_conn
    with get_conn() as conn:
        before = conn.execute(
            "SELECT id, name, amount, currency, due_date, recurring, paid, notes, document_id "
            "FROM bills WHERE id=?", (bill_id,)).fetchone()
    if not before:
        raise ValueError(f"bill {bill_id} not found")
    before_dict = dict(before)

    set_clause = ", ".join(f"{k}=?" for k in updates)
    params = list(updates.values()) + [bill_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE bills SET {set_clause} WHERE id=?", params)
        row = conn.execute(
            "SELECT id, name, amount, currency, due_date, recurring, paid, notes, document_id "
            "FROM bills WHERE id=?", (bill_id,)).fetchone()
        conn.commit()

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "bills", "highlight_id": bill_id,
             "reason": f"updated bill: {row['name']}"})

    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        before_for_rollback = {k: before_dict[k] for k in updates}
        pa.stage_with_rollback(
            skill="update_bill",
            rollback_kind="revert_bill_fields",
            rollback_args={"bill_id": bill_id, "before": before_for_rollback},
            preview={
                "action":  "update",
                "bill_id": bill_id,
                "before":  {k: before_dict[k] for k in (
                    "name", "amount", "currency", "due_date", "recurring", "paid", "notes", "document_id"
                )},
                "after":   {k: (updates.get(k) if k in updates else before_dict[k]) for k in (
                    "name", "amount", "currency", "due_date", "recurring", "paid", "notes", "document_id"
                )},
            },
            ctx=ctx,
        )

    return {"bill_id": bill_id, "bill": dict(row) if row else None}
