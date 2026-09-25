"""show_transactions — recent bank transactions from the local, synced
copy (never live FinTS — see backend/bank_sync.py)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional


async def execute(ctx, days: int = 30, account_id: Optional[int] = None,
                  category: Optional[str] = None) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        return {"_llm_hint": "No signed-in user — cannot show transactions."}

    from backend import spaces
    from backend.database import get_conn

    days = max(1, min(int(days or 30), 365))
    since = (date.today() - timedelta(days=days)).isoformat()

    frag, params = spaces.row_filter(user_id, getattr(ctx, "role", None), "bank_accounts",
                                     table_alias="a")
    q = (
        "SELECT t.booking_date, t.amount, t.currency, t.counterparty, t.purpose, t.category, "
        "       a.display_name AS account_name "
        "FROM bank_transactions t JOIN bank_accounts a ON a.id = t.account_id "
        f"WHERE {frag} AND t.booking_date >= ?"
    )
    params = list(params) + [since]
    if account_id is not None:
        q += " AND t.account_id = ?"
        params.append(account_id)
    if category is not None:
        q += " AND t.category = ?"
        params.append(category)
    q += " ORDER BY t.booking_date DESC LIMIT 200"

    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()

    txs = [dict(r) for r in rows]
    if not txs:
        return {"transactions": [], "_llm_hint": f"No transactions found for the last {days} days "
                                                  f"(with the given filters). Say so plainly; don't guess numbers."}
    return {"transactions": txs, "days": days,
            "_llm_hint": f"{len(txs)} transaction(s) over {days} days. Quote amounts/dates verbatim; "
                         f"if capped at 200, say the list was cut and offer a narrower range."}
