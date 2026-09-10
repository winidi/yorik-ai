"""finish_recording — stop the capture and start the transcript pipeline."""

from __future__ import annotations

from typing import Any, Dict, Optional


async def execute(ctx, recording_id: Optional[int] = None) -> Dict[str, Any]:
    from backend import recordings as R
    from backend.ui_tools import _append
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("finish_recording needs a signed-in user")
    if recording_id:
        row = R._row(int(recording_id))
        if not row or str(row["owner_user_id"]) != str(user_id):
            raise ValueError("no such recording of yours")
    else:
        row = R.latest_for(str(user_id), statuses=("recording",))
        if not row:
            return {"status": "none", "_llm_hint": "Nothing is being recorded right now; say so in one line."}
    rid = int(row["id"])
    if row["status"] != "recording":
        return {"recording_id": rid, "status": row["status"],
                "_llm_hint": "The recording was already ended; say so and offer recording_status."}
    _append({"type": "stop_recording", "recording_id": rid})
    source = getattr(ctx, "source", None) or "chat"
    # A device that is still capturing finishes by itself once it sees
    # stop_requested. Audio that is already complete on the server (API
    # upload, MCP) is finished here.
    has_audio = (R.rec_dir(rid) / "audio.webm").exists() or any(R.rec_dir(rid).glob("chunk-*.webm"))
    if has_audio and source.startswith("token:"):
        row = R.finish(rid)
        return {"recording_id": rid, "status": row["status"],
                "_llm_hint": "Say the transcript is being written and everybody at the table will be notified."}
    R.request_stop(rid)
    return {"recording_id": rid, "status": "recording", "stop_requested": True,
            "_llm_hint": "Say the recording is being stopped and the transcript takes a few minutes; everybody at the table gets a notification."}
