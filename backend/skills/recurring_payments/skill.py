"""recurring_payments — regular payments found in the bank bookings."""

from __future__ import annotations

from typing import Any, Optional


async def execute(ctx, from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict[str, Any]:
    if not getattr(ctx, "user_id", None):
        return {"_llm_hint": "No signed-in user — cannot look at bank bookings."}
    from backend import recurring
    from backend.payments import _iso
    out = recurring.find(ctx, since=_iso(from_date), until=_iso(to_date))
    if not out["recurring"]:
        out["_llm_hint"] = ("No regular payments found in the bank bookings"
                            + (f" since {out['bank_records_from']}" if out["bank_records_from"] else
                               "; there are no bank bookings for this person") + ". Say so plainly.")
        return out
    # wording approved by Dirk 2026-09-28
    out["_llm_hint"] = (f"Found in the bank bookings since {out['bank_records_from']}; payments rarer than monthly "
                        "show only once a year of bookings exists — say so when it matters.")
    return out
