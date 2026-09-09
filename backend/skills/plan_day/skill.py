"""plan_day — write an agreed day plan (tasks + Plan-calendar blocks)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


async def execute(ctx, date: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    from backend import day_plans as D
    from backend import pending_actions as pa
    from backend.ui_tools import _append
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("plan_day needs a signed-in user")
    plan_date = (date or "").strip()[:10]
    try:
        from datetime import date as _d
        _d.fromisoformat(plan_date)
    except Exception:
        raise ValueError("date must be YYYY-MM-DD")
    result = D.apply_plan(user_id=str(user_id), plan_date=plan_date, items=items or [])
    _append({"type": "refresh_data", "table": "tasks", "reason": f"day plan {plan_date}"})
    _append({"type": "show_calendar", "view": "day", "anchor_date": plan_date,
             "reason": f"plan for {plan_date}"})
    pending_id: Optional[str] = None
    if pa.should_confirm(ctx):
        pending_id = pa.stage_with_rollback(
            skill="plan_day",
            rollback_kind="unplan_day",
            rollback_args=result["rollback_args"],
            preview={
                "action": "plan_day",
                "date": plan_date,
                "items": [{"title": i["title"], "start": (i["start"] or "")[11:16] or None,
                           "end": (i["end"] or "")[11:16] or None} for i in result["items"]],
                **result["summary"],
            },
            ctx=ctx,
        )
    return {
        "summary": result["summary"],
        "date": plan_date,
        "items": result["items"],
        "pending_id": pending_id,
        "_llm_hint": "Confirm in ONE short line what was written (n tasks, n blocks). The card can undo the whole day.",
    }
