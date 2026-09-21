"""file_attachment — a chat attachment goes to Paperless."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional


async def execute(ctx, attachment_id: int, visibility: Optional[str] = None,
                  title: Optional[str] = None) -> Dict[str, Any]:
    from backend import chat_attachments as A
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("file_attachment needs a signed-in user")
    result = await asyncio.to_thread(A.file_in_paperless, int(attachment_id), str(user_id), visibility, title)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"),
                "_llm_hint": f"REJECTED: {result.get('error')}. Tell the user in one line; nothing was filed."}
    from backend.ui_tools import _append
    _append({"type": "refresh_data", "table": "chat_attachments", "highlight_id": int(attachment_id),
             "reason": "attachment filed in Paperless"})
    if result.get("already_filed"):
        return {"ok": True, "already_filed": True, "_llm_hint": "It was already filed in Paperless. Say so in one line."}
    who = {"private": "only the user", "parents": "the parents, not the children", "shared": "everyone in the household",
           "business": "the business group"}.get(result.get("visibility") or "", "only the user")
    return {"ok": True, "visibility": result.get("visibility"),
            "_llm_hint": f"Filed; visible to {who}. Tell the user in one line that it is in Paperless now "
                         f"(searchable in about a minute, after OCR) and who can see it. Nothing to confirm."}
