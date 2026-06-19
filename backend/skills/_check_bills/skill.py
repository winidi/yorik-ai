"""check_bills skill — date-bounded read of the bills table."""

from __future__ import annotations

from typing import Any, Optional


async def execute(
    ctx,
    start_iso: Optional[str] = None,
    end_iso: Optional[str] = None,
    include_paid: bool = False,
) -> dict[str, Any]:
    from backend.database import get_conn

    start_date = (start_iso or "")[:10] or None
    end_date   = (end_iso   or "")[:10] or None

    where: list[str] = []
    params: list[Any] = []
    if not include_paid:
        where.append("paid = 0")
    if start_date:
        where.append("due_date >= ?"); params.append(start_date)
    if end_date:
        where.append("due_date <= ?"); params.append(end_date)

    sql = ("SELECT id, name, amount, currency, due_date, recurring, paid, "
           "       email_message_id, document_id FROM bills")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY due_date ASC LIMIT 25"

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    out: dict[str, Any] = {
        "bills":  rows,
        "window": {"start_iso": start_iso, "end_iso": end_iso},
        "count":  len(rows),
    }
    # Anti-enumeration rule, paired with the wording in skill.md. The
    # audit caught the LLM listing every bill (name + amount + due date)
    # in prose despite the .md rule — the rule only sticks when it's an
    # _llm_hint the model sees inline with the result.
    if rows:
        out["_llm_hint"] = (
            f"shown_to_user:{len(rows)} bill(s) in window. Reply ONE short "
            f"sentence with the count + 'siehe Karten unten' (or equivalent "
            f"in the user's language). Do NOT enumerate names, amounts, or "
            f"due dates in your text — the bills card carries those. If the "
            f"user asks 'zeig mir die Rechnung' next: use document_id for "
            f"read_document if set, else fall back to find_document."
        )
    return out
