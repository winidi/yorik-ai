"""add_bill skill — apply-then-confirm INSERT on bills (backend/bills.py)."""
from __future__ import annotations
from typing import Any, Optional


async def execute(
    ctx,
    name: str,
    amount: float,
    currency: Optional[str] = None,
    due_date: Optional[str] = None,
    payee: Optional[str] = None,
    number: Optional[str] = None,
    recurring: Optional[str] = None,
    notes: Optional[str] = None,
    source: Optional[str] = None,
    source_ref: Optional[int] = None,
    email_message_id: Optional[int] = None,
    document_id: Optional[int] = None,
) -> dict[str, Any]:
    from backend import bills

    # Bills have date-granularity dues; trim any time component the
    # LLM appended (e.g. "2026-06-03T17:00:00").
    if due_date:
        due_date = str(due_date)[:10]
    row = bills.create_bill(
        user_id=getattr(ctx, "user_id", None), name=name, amount=amount, currency=currency,
        due_date=due_date, payee=payee, number=number, recurring=recurring, notes=notes,
        source=source or "chat", source_ref=source_ref,
        email_message_id=email_message_id, document_id=document_id,
    )
    bill_id = int(row["id"])

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "bills", "highlight_id": bill_id,
             "reason": f"created bill: {row['name']}"})

    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="add_bill",
            rollback_kind="delete_bill",
            rollback_args={"bill_id": bill_id},
            preview={
                "action":    "create",
                "bill_id":   bill_id,
                "name":      row["name"],
                "payee":     row.get("payee"),
                "amount":    row["amount"],
                "currency":  row["currency"],
                "due_date":  row["due_date"],
                "recurring": row.get("recurring"),
                "notes":     row.get("notes"),
            },
            ctx=ctx,
        )

    return {"bill_id": bill_id, "bill": row}
