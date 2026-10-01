"""find_bill_by_name — LLM-internal resolver from bill name → bill_id.

Complement to check_bills (which renders cards but hides names + IDs
from the LLM). Returns minimal rows for the LLM to act on with
update_bill / delete_bill.
"""

from __future__ import annotations

from typing import Any


async def execute(
    ctx,
    query: str,
    include_paid: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    from backend import bills

    q = (query or "").strip()
    if not q:
        return {"matches": [], "count": 0}
    limit = max(1, min(int(limit or 10), 50))

    # Token-AND matching over name and payee — see find_task_by_title
    # for the rationale. Only bills the person may see.
    tokens = [t.lower() for t in q.split() if t]
    rows = bills.list_bills(getattr(ctx, "user_id", None), getattr(ctx, "role", None),
                            "all" if include_paid else "open", limit=500)
    hits = []
    for r in rows:
        hay = f"{r.get('name') or ''} {r.get('payee') or ''}".lower()
        if all(t in hay for t in tokens):
            hits.append({k: r.get(k) for k in ("id", "name", "payee", "amount", "currency",
                                                "due_date", "paid", "recurring")})
    hits.sort(key=lambda r: (bool(r["paid"]), r.get("due_date") or "9999", -int(r["id"])))
    hits = hits[:limit]
    return {"matches": hits, "count": len(hits)}
