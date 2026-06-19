"""find_bill_by_name — LLM-internal resolver from bill name → bill_id.

Complement to check_bills (which renders cards but hides names + IDs
from the LLM). Returns minimal rows for the LLM to act on with
update_bill / delete_bill / mark_bill_paid.
"""

from __future__ import annotations

from typing import Any


async def execute(
    ctx,
    query: str,
    include_paid: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    from backend.database import get_conn

    q = (query or "").strip()
    if not q:
        return {"matches": [], "count": 0}
    limit = max(1, min(int(limit or 10), 50))

    # Token-AND matching — see find_task_by_title for the rationale.
    where: list[str] = []
    params: list[Any] = []
    tokens = [t for t in q.split() if t]
    for tok in tokens:
        where.append("lower(name) LIKE ?")
        params.append(f"%{tok.lower()}%")
    if not include_paid:
        where.append("paid = 0")

    sql = (
        "SELECT id, name, amount, currency, due_date, paid, recurring "
        "FROM bills WHERE " + " AND ".join(where) + " "
        "ORDER BY paid ASC, due_date IS NULL, due_date ASC, id DESC "
        "LIMIT ?"
    )
    params.append(limit)

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    return {"matches": rows, "count": len(rows)}
