"""plan_my_day — collect what planning a day needs."""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Dict, Optional


async def execute(ctx, date: Optional[str] = None, ask_agent: bool = True,
                  request: Optional[str] = None) -> Dict[str, Any]:
    from backend import day_plans as D
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("plan_my_day needs a signed-in user")
    plan_date = (date or "").strip()[:10] or _date.today().isoformat()
    _date.fromisoformat(plan_date)
    context = D.context_for(str(user_id), plan_date, role=getattr(ctx, "role", "member"))
    existing = D.get_plan(str(user_id), plan_date)
    out: Dict[str, Any] = {
        "context": context,
        "draft": (existing or {}).get("draft"),
        "draft_status": (existing or {}).get("status"),
    }
    if ask_agent:
        try:
            q = (f"Ich plane meinen Tag für {plan_date} ({context['weekday']}). "
                 f"Gib mir in höchstens 8 kurzen Stichpunkten: das heutige Briefing aus dem Homebase, "
                 f"offene Punkte und Fristen, die heute zählen. Keine Einleitung.")
            if request:
                q += f" Anlass: {request.strip()[:300]}"
            res = await ctx.call_skill("ask_agent", question=q)
            if res.get("answer"):
                out["outside_context"] = res["answer"]
            elif res.get("error"):
                out["outside_error"] = res["error"]
        except Exception as exc:  # noqa: BLE001
            out["outside_error"] = f"{type(exc).__name__}: {exc}"
    D.save_draft(str(user_id), plan_date, ((existing or {}).get("draft") or {}).get("items") or [],
                 context={k: context[k] for k in ("fixed_events", "open_tasks", "carry_over")})
    out["_llm_hint"] = (
        "Draft 3–8 items around fixed_events (never move those), carry_over first, 2–4 focus blocks with "
        "HH:MM times at most, the rest as plain tasks. Show the draft as a numbered list and ask what to change. "
        "Do not call plan_day until the user says it is good."
    )
    return out
