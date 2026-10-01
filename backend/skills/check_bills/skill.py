"""check_bills skill — date-bounded read of the bills the person may see."""

from __future__ import annotations

from typing import Any, Optional


async def execute(
    ctx,
    start_iso: Optional[str] = None,
    end_iso: Optional[str] = None,
    include_paid: bool = False,
) -> dict[str, Any]:
    from backend import bills

    start_date = (start_iso or "")[:10] or None
    end_date   = (end_iso   or "")[:10] or None

    # Own bills and the Finance space's, like the Finance tab — never
    # the whole table (chat visibility audit 2026-09-25).
    rows = bills.list_bills(getattr(ctx, "user_id", None), getattr(ctx, "role", None),
                            "all" if include_paid else "open", limit=200)
    if start_date:
        rows = [r for r in rows if (r.get("due_date") or "") >= start_date]
    if end_date:
        rows = [r for r in rows if (r.get("due_date") or "") <= end_date]
    rows = rows[:25]
    keep = ("id", "name", "payee", "amount", "currency", "due_date", "recurring", "paid", "paid_at",
            "paid_by", "overdue", "days_left", "source", "source_ref", "email_message_id", "document_id", "link")
    rows = [{k: r.get(k) for k in keep} for r in rows]

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
