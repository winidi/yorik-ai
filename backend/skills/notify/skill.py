"""notify — a bell entry plus push for the calling user."""

from __future__ import annotations

from typing import Any, Dict, Optional


async def execute(ctx, title: str, body: Optional[str] = None, url: Optional[str] = None) -> Dict[str, Any]:
    from backend import notifications as _notif
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("notify needs a signed-in user")
    title = (title or "").strip()[:140]
    if not title:
        raise ValueError("title is required")
    body = (body or "").strip()[:600] or None
    url = (url or "").strip() or None
    if url and not (url.startswith("/") or url.startswith("http://") or url.startswith("https://")):
        raise ValueError("url must be a Yorik route (/r/…) or an http(s) link")
    source = getattr(ctx, "source", None) or "chat"
    nid = _notif.create(user_id=str(user_id), kind="agent_message", title=title, body=body,
                        payload={"source": source}, navigate_to=url)
    pushed = _notif.last_push_count(nid)
    return {"notification_id": nid, "pushed_devices": pushed,
            "_llm_hint": "Confirm in one short line that the note was left in Yorik."}
