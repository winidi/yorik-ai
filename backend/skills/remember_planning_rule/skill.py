"""remember_planning_rule — append one sentence to the person's planning rules."""

from __future__ import annotations

from typing import Any, Dict


async def execute(ctx, rule: str) -> Dict[str, Any]:
    from backend import day_plans as D
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("remember_planning_rule needs a signed-in user")
    rules = D.add_planning_rule(str(user_id), rule)
    return {"rules": rules, "_llm_hint": "Confirm in one short line that it is remembered for future plans."}
