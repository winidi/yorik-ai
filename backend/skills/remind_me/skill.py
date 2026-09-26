"""remind_me — a bell entry with push at a later moment (backend/reminders.py)."""

from __future__ import annotations

from typing import Any, Dict, Optional


async def execute(ctx, title: str, at: Optional[str] = None, in_minutes: Optional[int] = None,
                  body: Optional[str] = None) -> Dict[str, Any]:
    from backend import reminders
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("remind_me needs a signed-in user")
    title = (title or "").strip()[:140]
    if not title:
        raise ValueError("title is required: what should the reminder say?")
    due = reminders.parse_when(at, in_minutes)
    rid = reminders.create(str(user_id), title, due, (body or "").strip()[:600] or None)
    local = due.astimezone(reminders.household_tz())
    return {"reminder_id": rid, "due_local": local.strftime("%Y-%m-%d %H:%M"),
            "weekday": local.strftime("%A"),
            "_llm_hint": (f"Reminder set for {local.strftime('%A %Y-%m-%d %H:%M')} (household time). "
                          "Confirm in one short line with exactly this day and time.")}
