"""add_bill skill — apply-then-confirm INSERT on bills."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Optional


async def execute(
    ctx,
    name: str,
    amount: float,
    currency: str = "EUR",
    due_date: Optional[str] = None,
    recurring: Optional[str] = None,
    notes: Optional[str] = None,
    email_message_id: Optional[int] = None,
    document_id: Optional[int] = None,
) -> dict[str, Any]:
    if not name or not name.strip():
        raise ValueError("name is required")
    try:
        amount_f = float(amount)
    except (TypeError, ValueError) as e:
        raise ValueError(f"amount must be a number: {e}")
    if due_date:
        try:
            datetime.strptime(due_date[:10], "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"due_date must be YYYY-MM-DD: {e}")
        # Bills have date-granularity dues; trim any time component the
        # LLM appended (e.g. "2026-06-03T17:00:00") so downstream code
        # never sees a half-ISO string.
        due_date = due_date[:10]

    # Phase B: bills live in the Finance shared space by default.
    # Lookup once; falls back to NULL only on a misconfigured fresh
    # install (no Finance space yet — that's an installer bug, not
    # something to swallow silently).
    from backend.database import get_conn, conn_ctx, DEFAULT_DB_PATH as _DB
    with conn_ctx(_DB) as _c:
        _row = _c.execute(
            "SELECT id FROM spaces WHERE slug='finance' LIMIT 1"
        ).fetchone()
        finance_space_id = int(_row["id"]) if _row else None
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bills (name, amount, currency, due_date, recurring, paid, notes, email_message_id, document_id, space_id) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (name.strip(), amount_f, currency, due_date, recurring, notes, email_message_id, document_id, finance_space_id),
        )
        bill_id = cur.lastrowid
        row = conn.execute(
            "SELECT id, name, amount, currency, due_date, recurring, paid, notes, space_id, email_message_id, document_id "
            "FROM bills WHERE id=?", (bill_id,)).fetchone()
        conn.commit()

    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "bills", "highlight_id": bill_id,
             "reason": f"created bill: {name.strip()}"})

    from backend import pending_actions as pa
    if pa.should_confirm(ctx):
        pa.stage_with_rollback(
            skill="add_bill",
            rollback_kind="delete_bill",
            rollback_args={"bill_id": bill_id},
            preview={
                "action":    "create",
                "bill_id":   bill_id,
                "name":      name.strip(),
                "amount":    amount_f,
                "currency":  currency,
                "due_date":  due_date,
                "recurring": recurring,
                "notes":     notes,
            },
            ctx=ctx,
        )

    return {"bill_id": bill_id, "bill": dict(row) if row else None}


