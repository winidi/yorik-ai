"""start_recording — create the recording row and tell the UI to capture."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional


def resolve_members(names: List[str], exclude_id: Optional[str] = None) -> tuple[list, list]:
    """Household members by name (case-insensitive, prefix ok) → ([{user_id, name}], [unknown])."""
    from backend.database import get_conn
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name FROM user_profiles WHERE role IN ('platform_admin','admin','member','restricted')").fetchall()
    members = [{"user_id": str(r["id"]), "name": r["name"] or ""} for r in rows]
    found, unknown = [], []
    for raw in names or []:
        n = (raw or "").strip().lower()
        if not n:
            continue
        hit = next((m for m in members if m["name"].lower() == n), None) or \
              next((m for m in members if m["name"].lower().startswith(n)), None)
        if hit and hit["user_id"] != str(exclude_id) and hit not in found:
            found.append(hit)
        elif not hit:
            unknown.append(raw)
    return found, unknown


async def execute(ctx, title: Optional[str] = None, kind: Optional[str] = None,
                  participants: Optional[List[str]] = None) -> Dict[str, Any]:
    from backend import recordings as R
    from backend.ui_tools import _append
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("start_recording needs a signed-in user")
    if isinstance(participants, str):
        participants = [p for p in participants.replace(" und ", ",").replace(" and ", ",").split(",")]
    found, unknown = resolve_members(participants or [], exclude_id=str(user_id))
    kind = (kind or "conversation").strip().lower()
    if kind not in R.KINDS:
        kind = "conversation"
    if not (title or "").strip():
        title = {"dinner": "Dinner", "meeting": "Meeting"}.get(kind, "Recording") + " " + date.today().strftime("%d.%m.")
    active = R.latest_for(str(user_id), statuses=("recording",))
    if active:
        return {"recording_id": active["id"], "already_running": True,
                "_llm_hint": "A recording is already running; say so and offer to stop it with finish_recording."}
    row = R.create(str(user_id), title=title.strip(), kind=kind, participants=[m["user_id"] for m in found])
    _append({"type": "start_recording", "recording_id": row["id"], "title": row["title"]})
    out = {"recording_id": row["id"], "title": row["title"], "kind": kind, "participants": found, "unknown": unknown}
    if unknown:
        out["_llm_hint"] = "Recording started. Ask which household members the unknown names meant; they can be added by restarting."
    else:
        out["_llm_hint"] = "Say in one line that the recording runs and that 'the dinner is over' (or the Stop button) ends it."
    return out
