"""day_review — plan versus reality for one day."""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Dict, Optional


async def execute(ctx, date: Optional[str] = None) -> Dict[str, Any]:
    from backend import day_plans as D
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("day_review needs a signed-in user")
    plan_date = (date or "").strip()[:10] or _date.today().isoformat()
    _date.fromisoformat(plan_date)
    review = D.review_day(str(user_id), plan_date)
    review["_llm_hint"] = (
        "Write 3–5 warm, concrete sentences: what got done (name them), time over/under estimate if "
        "actual_minutes is known, what carries over. No list of failures."
    )
    return review
