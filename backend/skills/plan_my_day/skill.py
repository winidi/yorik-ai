"""plan_my_day — collect what planning a day needs."""

from __future__ import annotations

import re
from datetime import date as _date
from typing import Any, Dict, List, Optional

# What Yorik asks the person's own agent. A fixed shape so the answer
# can be parsed into candidates; the agent knows the person's files and
# notes (Homebase), Yorik does not and should not.
AGENT_QUESTION = (
    "Ich plane meinen Tag für {date} ({weekday}). Nenne mir bis zu 10 Kandidaten aus meinen Notizen, "
    "Dateien und dem Backlog auf dem Rechner (Homebase), die heute sinnvoll wären. Meine Yorik-Aufgaben "
    "und den Yorik-Kalender kenne ich schon, die nicht nennen und dafür keine Yorik-Tools aufrufen. "
    "Antworte NUR mit Zeilen im Format\n- Titel | Minuten | warum (ein Satz)\n"
    "Keine Einleitung, kein Fazit. Fristen zuerst.{request}"
)

_LINE = re.compile(r"^\s*[-*•]\s*(?P<title>[^|]+?)\s*\|\s*(?P<min>\d{1,3})?\s*(?:min|Min\.?|Minuten)?\s*\|?\s*(?P<why>.*)$")


def parse_candidates(text: str) -> List[Dict[str, Any]]:
    """'- Titel | 45 | warum' lines → [{title, estimated_minutes, why}]."""
    out: List[Dict[str, Any]] = []
    for raw in (text or "").splitlines():
        m = _LINE.match(raw)
        if not m or not m.group("title").strip():
            continue
        mins = m.group("min")
        out.append({"title": m.group("title").strip()[:200],
                    "estimated_minutes": int(mins) if mins else None,
                    "why": (m.group("why") or "").strip().strip("|").strip()[:300]})
    return out[:10]


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
            q = AGENT_QUESTION.format(
                date=plan_date, weekday=context["weekday"],
                request=(f" Anlass: {request.strip()[:300]}" if request else ""))
            res = await ctx.call_skill("ask_agent", question=q)
            if res.get("answer"):
                known = {t["title"].strip().lower() for t in context.get("open_tasks", [])}
                known |= {t["title"].strip().lower() for t in context.get("report_candidates", [])}
                cands = [c for c in parse_candidates(res["answer"]) if c["title"].strip().lower() not in known]
                if cands:
                    out["agent_candidates"] = cands
                else:
                    out["outside_context"] = res["answer"]
            elif res.get("error"):
                # no agent for this person, or the workstation is off: plan from Yorik's data
                out["outside_error"] = res["error"]
        except Exception as exc:  # noqa: BLE001
            out["outside_error"] = f"{type(exc).__name__}: {exc}"
    D.save_draft(str(user_id), plan_date, ((existing or {}).get("draft") or {}).get("items") or [],
                 context={k: context[k] for k in ("fixed_events", "open_tasks", "carry_over")})
    out["_full_output"] = True      # the whole context, not the 1500-char card
    out["_llm_hint"] = (
        "Two layers: (1) up to 4 time blocks with HH:MM around fixed_events (never move those), fitted into "
        "free_minutes; (2) today's list: every open task the person should do today, without times — as long "
        "as it needs to be, carry_over first. Follow rules. Offer report_candidates and agent_candidates as "
        "suggestions marked with their source; a chosen candidate goes into the plan with its report_ref or as "
        "a new item. End with what stays in the backlog (backlog.open_total minus planned) in one line. "
        "Show the draft as a numbered list and ask what to change; do not call plan_day until the user says "
        "it is good. When the user corrects who does what or a habit, ask once whether to remember it and "
        "call remember_planning_rule."
    )
    return out
