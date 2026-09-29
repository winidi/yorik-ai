"""spending_summary — totals by category over a period, from the local
synced copy. Aggregation only; for the actual rows use show_transactions."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional


async def execute(ctx, days: int = 30, account_id: Optional[int] = None,
                  from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        return {"_llm_hint": "No signed-in user — cannot summarise spending."}

    from backend import spaces
    from backend.database import get_conn

    days = max(1, min(int(days or 30), 365))
    from backend.skills.show_transactions.skill import _window, ROUNDUP_SQL
    since, until = _window(days, from_date, to_date)

    frag, params = spaces.row_filter(user_id, getattr(ctx, "role", None), "bank_accounts",
                                     table_alias="a")
    q = (
        # Round-ups go to savings, not to the shop's category (2026-09-28)
        f"SELECT CASE WHEN {ROUNDUP_SQL} THEN 'Savings (round-ups)' "
        "            ELSE COALESCE(t.category, 'unkategorisiert') END AS category, "
        "       SUM(t.amount) AS total, COUNT(*) AS n "
        "FROM bank_transactions t JOIN bank_accounts a ON a.id = t.account_id "
        f"WHERE {frag} AND t.booking_date >= ?"
    )
    params = list(params) + [since]
    if until:
        q += " AND t.booking_date <= ?"
        params.append(until)
    if account_id is not None:
        q += " AND t.account_id = ?"
        params.append(account_id)
    q += " GROUP BY 1 ORDER BY total ASC"

    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()

    from backend.finance_categories import english
    by_category = [dict(r, category=english(r["category"])) for r in rows]
    if not by_category:
        return {"by_category": [], "_llm_hint": f"No transactions in the last {days} days to summarise. "
                                                 f"Say so; don't invent numbers."}
    total_out = sum(r["total"] for r in by_category if r["total"] < 0)
    total_in = sum(r["total"] for r in by_category if r["total"] > 0)
    return {
        "by_category": by_category,
        "days": days,
        "total_outgoing": total_out,
        "total_incoming": total_in,
        "_llm_hint": (
            f"Sums over {days} days, negative = outgoing. A large 'Uncategorised' bucket means many "
            f"transactions don't match a rule yet — say so honestly rather than presenting it as complete."
        ),
    }
