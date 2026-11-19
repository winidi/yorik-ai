"""file_email_attachment — an email's attachment goes to Paperless."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

_WHO = {"private": "only the user", "parents": "the parents, not the children",
        "shared": "everyone in the household", "business": "the business group"}


def _attachments(message_id: int, user_id: str) -> Optional[list]:
    """The email's attachments, or None when it is not the user's."""
    from backend.database import get_conn
    with get_conn() as conn:
        mail = conn.execute("SELECT id, subject FROM email_messages WHERE id = ? AND owner_user_id = ?",
                            (message_id, user_id)).fetchone()
        if not mail:
            return None
        return [dict(r) for r in conn.execute(
            "SELECT id, filename FROM email_attachments WHERE message_id = ? ORDER BY id", (message_id,)).fetchall()]


def pick(atts: list, filename: Optional[str]) -> list:
    """The attachments `filename` points at: the exact name, else those
    containing it; all of them when no name was given."""
    want = (filename or "").strip().lower()
    if not want:
        return atts
    exact = [a for a in atts if (a["filename"] or "").lower() == want]
    return exact or [a for a in atts if want in (a["filename"] or "").lower()]


async def execute(ctx, message_id: int, filename: Optional[str] = None,
                  visibility: Optional[str] = None) -> Dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("file_email_attachment needs a signed-in user")
    atts = await asyncio.to_thread(_attachments, int(message_id), str(user_id))
    if atts is None:
        return {"ok": False, "_llm_hint": "REJECTED: there is no email with this id for this user. Nothing was filed."}
    if not atts:
        return {"ok": False, "_llm_hint": "REJECTED: this email has no attachments. Tell the user in one line."}
    chosen = pick(atts, filename)
    names = [a["filename"] for a in atts]
    if len(chosen) != 1:
        return {"ok": False, "attachments": names,
                "_llm_hint": ("REJECTED: say which attachment with `filename`; this email has: "
                              + ", ".join(names) + ". Ask the user if it is not clear which one they mean.")}
    att = chosen[0]
    from backend import email_routes
    result = await asyncio.to_thread(email_routes.file_attachment, int(att["id"]), str(user_id), visibility)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error"),
                "_llm_hint": f"REJECTED: {result.get('error')}. Tell the user in one line; nothing was filed."}
    from backend import file_links
    if file_links.for_outside_agent(ctx):
        from backend.ui_tools import _append
        _append({"type": "document_filed", "filename": att["filename"], "visibility": result.get("visibility"),
                 "already_filed": bool(result.get("already_filed")),
                 "actions": [file_links.view_action(str(user_id), "email_attachment", int(att["id"]))]})
    if result.get("already_filed"):
        return {"ok": True, "already_filed": True, "filename": att["filename"],
                "_llm_hint": "It was already in Paperless. Say so in one line."}
    who = _WHO.get(result.get("visibility") or "", "only the user")
    return {"ok": True, "filename": att["filename"], "visibility": result.get("visibility"),
            "_llm_hint": f"Filed; visible to {who}. Tell the user in one line that it is in Paperless now "
                         f"(searchable in about a minute, after OCR) and who can see it. Nothing to confirm."}
