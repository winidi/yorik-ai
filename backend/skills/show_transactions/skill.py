"""show_transactions — recent bank transactions from the local, synced
copy (never live FinTS — see backend/bank_sync.py)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional


async def execute(ctx, days: int = 30, account_id: Optional[int] = None,
                  category: Optional[str] = None, search: Optional[str] = None,
                  from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        return {"_llm_hint": "No signed-in user — cannot show transactions."}

    from backend import spaces
    from backend.database import get_conn

    days = max(1, min(int(days or 30), 365))
    since, until = _window(days, from_date, to_date)

    frag, params = spaces.row_filter(user_id, getattr(ctx, "role", None), "bank_accounts",
                                     table_alias="a")
    q = (
        "SELECT t.booking_date, t.amount, t.currency, t.counterparty, t.purpose, t.category, "
        "       a.display_name AS account_name "
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
    if category:
        # "Abos" finds "Verträge & Abos" — the model guessed short names
        # and got nothing back (chat test 2026-09-26).
        q += " AND LOWER(COALESCE(t.category, '')) LIKE ?"
        params.append(f"%{category.strip().lower()}%")
    if search:
        like = f"%{search.strip().lower()}%"
        q += " AND (LOWER(COALESCE(t.counterparty, '')) LIKE ? OR LOWER(COALESCE(t.purpose, '')) LIKE ?)"
        params.extend([like, like])
    q += " ORDER BY t.booking_date DESC LIMIT 200"

    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()

    txs = [dict(r) for r in rows]
    if not txs:
        return {"transactions": [], "_llm_hint": f"No transactions found for the last {days} days "
                                                  f"(with the given filters). Say so plainly; don't guess numbers."
                                                  + _categories_note(user_id, getattr(ctx, "role", None))}
    # Totals come from here, not from the model: in the replay the model
    # listed all seven transfers correctly and then added them up wrong.
    amounts = [float(t["amount"] or 0) for t in txs]
    # Totals first: when the row list is cut for the model, they survive.
    return {"count": len(txs), "total": round(sum(amounts), 2),
            "total_outgoing": round(sum(a for a in amounts if a < 0), 2),
            "total_incoming": round(sum(a for a in amounts if a > 0), 2),
            "days": days, "transactions": txs,
            "_llm_hint": f"{len(txs)} transaction(s) over {days} days. Quote amounts/dates verbatim; "
                         f"if capped at 200, say the list was cut and offer a narrower range. "
                         f"Totals are already computed (count, total, total_outgoing, total_incoming) — "
                         f"quote them, don't add up rows yourself."}


def _window(days: int, from_date: Optional[str], to_date: Optional[str]) -> tuple[str, Optional[str]]:
    """(since, until) as ISO dates. from_date/to_date win over days —
    "im September" is the calendar month, not the last 30 days."""
    def _iso(v: Optional[str]) -> Optional[str]:
        try:
            return date.fromisoformat(str(v).strip()[:10]).isoformat() if v else None
        except ValueError:
            return None
    since = _iso(from_date) or (date.today() - timedelta(days=days)).isoformat()
    return since, _iso(to_date)


def _categories_note(user_id: Any, role: Optional[str]) -> str:
    """The categories that exist, for the empty-result hint."""
    from backend import spaces
    from backend.database import get_conn
    frag, params = spaces.row_filter(user_id, role, "bank_accounts", table_alias="a")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT t.category FROM bank_transactions t JOIN bank_accounts a ON a.id = t.account_id "
            f"WHERE {frag} AND t.category IS NOT NULL ORDER BY t.category", list(params)).fetchall()
    names = [r["category"] for r in rows if r["category"]]
    return f" Categories that exist: {', '.join(names)}." if names else ""
