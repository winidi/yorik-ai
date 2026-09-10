"""recording_report — the report of a recording, generated once, reused after."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional


async def execute(ctx, recording_id: Optional[int] = None, template: Optional[str] = None,
                  refresh: Optional[bool] = False) -> Dict[str, Any]:
    from backend import recordings as R
    from backend import recording_reports as REP
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("recording_report needs a signed-in user")
    user = {"id": str(user_id), "role": getattr(ctx, "role", None)}
    if recording_id:
        row = R.can_view(int(recording_id), user)
        if not row:
            raise ValueError("no such recording, or you were not part of it")
    else:
        row = next((R._row(r["id"]) for r in R.list_visible(user, limit=20) if r["status"] == "done"), None)
        if not row:
            return {"status": "none", "_llm_hint": "There is no finished recording the user may see; say so in one line."}
    rid = int(row["id"])
    if row["status"] != "done":
        return {"recording_id": rid, "status": row["status"], "progress": row.get("progress"),
                "_llm_hint": "The transcript is not finished yet; say which step is running and to ask again in a few minutes."}
    report = None if refresh else REP.get_report(rid)
    generated = False
    if report is None:
        template = (template or "").strip().lower() or None
        report = await asyncio.get_running_loop().run_in_executor(
            None, lambda: REP.build_report(rid, template, notify=not REP.get_report(rid)))
        generated = True
    return {
        "recording_id": rid, "title": row["title"], "kind": row["kind"], "recorded_on": (row.get("started_at") or "")[:10],
        "participants": list(R._names(R._participants(row) + [str(row["owner_user_id"])]).values()),
        "report": report, "generated": generated,
        "_llm_hint": "Present the report section by section in the user's language; number the tasks and offer to add them with add_task.",
    }
