"""cancel_reminder: list the caller's open reminders, or cancel one
(backend/reminders.py). Only the caller's own, never anyone else's."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def _open(user_id: str) -> list[dict[str, Any]]:
    from backend import reminders
    from backend.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, due_at FROM reminders WHERE user_id = ? AND fired_at IS NULL "
            "AND cancelled_at IS NULL ORDER BY due_at LIMIT 50", (user_id,)).fetchall()
    tz = reminders.household_tz()
    out = []
    for r in rows:
        due = r["due_at"]
        try:
            due = (due if isinstance(due, datetime) else datetime.fromisoformat(str(due).replace("Z", "+00:00")))
            due_local = due.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            due_local = str(due)
        out.append({"id": int(r["id"]), "title": r["title"], "due_local": due_local})
    return out


async def execute(ctx, reminder_id: Optional[int] = None) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("cancel_reminder needs a signed-in user")
    user_id = str(user_id)
    if reminder_id in (None, ""):
        found = _open(user_id)
        return {"reminders": found, "count": len(found),
                "_llm_hint": "No reminder was cancelled. Ask which one the user means if it is not clear, "
                             "then call again with its id."}
    from backend import reminders
    before = {r["id"]: r for r in _open(user_id)}
    if not reminders.cancel(int(reminder_id), user_id):
        return {"cancelled": False, "reminders": list(before.values()),
                "_llm_hint": f"Reminder {reminder_id} is not an open reminder of this user (rung already, "
                             "cancelled, or someone else's). Nothing was changed."}
    gone = before.get(int(reminder_id)) or {"id": int(reminder_id)}
    return {"cancelled": True, "reminder": gone,
            "_llm_hint": "Cancelled; it will not ring. Confirm it to the user in one short line."}
