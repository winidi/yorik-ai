"""recording_status — state and transcript of a recording the user may see."""

from __future__ import annotations

from typing import Any, Dict, Optional

MAX_TRANSCRIPT_CHARS = 12_000


async def execute(ctx, recording_id: Optional[int] = None) -> Dict[str, Any]:
    from backend import recordings as R
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("recording_status needs a signed-in user")
    user = {"id": str(user_id), "role": getattr(ctx, "role", None)}
    if recording_id:
        row = R.can_view(int(recording_id), user)
        if not row:
            raise ValueError("no such recording, or you were not part of it")
    else:
        rows = R.list_visible(user, limit=1)
        if not rows:
            return {"status": "none", "_llm_hint": "There is no recording the user may see; say so in one line."}
        row = R._row(rows[0]["id"])
    info = R.public(row)
    out: Dict[str, Any] = {
        "recording_id": info["id"], "title": info["title"], "kind": info["kind"], "status": info["status"],
        "progress": info["progress"], "error": info["error"], "started_at": info["started_at"],
        "duration_s": info["duration_s"], "participants": [p["name"] for p in info["participants"]],
        "owner": info["owner_name"], "has_report": info["has_report"],
    }
    out["_full_output"] = True      # the transcript must reach the model whole
    if info["status"] == "done":
        segs = R.segments(info["id"])
        out["speakers"] = sorted({s["speaker"] for s in segs})
        out["turns"] = len(segs)
        out["transcript"] = R.transcript_text(info["id"], max_chars=MAX_TRANSCRIPT_CHARS)
        out["_llm_hint"] = "Answer from the transcript; when the user only asked for the status, summarise who spoke and how long it is."
    elif info["status"] in ("uploaded", "processing"):
        out["_llm_hint"] = "Say the transcript is still being written and name the step in progress."
    elif info["status"] == "recording":
        out["_llm_hint"] = "Say the recording is still running and can be ended with 'the dinner is over'."
    else:
        out["_llm_hint"] = "Say the recording failed and quote the error in plain words."
    return out
