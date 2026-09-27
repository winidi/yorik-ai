"""payments_to — everything paid to one payee, receipts matched to bookings."""

from __future__ import annotations

from typing import Any, Optional


async def execute(ctx, payee: Optional[str] = None, also: Optional[list] = None,
                  from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict[str, Any]:
    if not getattr(ctx, "user_id", None):
        return {"_llm_hint": "No signed-in user — cannot look up payments."}
    from backend.payments import payments_to
    out = await payments_to(ctx, payee, also=also, from_date=from_date, to_date=to_date)
    if not out["payments"]:
        what = ", ".join(out["searched_as"]) if out["searched_as"] else "an unpaid bill"
        out["_llm_hint"] = (f"No booking and no receipt for {what} between "
                            f"{out['from']} and {out['to']}. Say so plainly; don't guess.")
        return out
    out["_llm_hint"] = ("Totals are computed; quote them. Name each payment once. "
                        "Say which were paid another way than this account.")
    return out
