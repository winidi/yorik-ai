"""send_prepared_email — send the email prepare_email staged, for an
outside agent whose user said to send it (Send on the card in a client
such as Brepper, or "send it" in the conversation).

Sends exactly what the card showed: the staged draft of this person.
The recipient the agent names must match it, so a draft that replaced
an older one is never sent in its place. Yorik's own chat keeps sending
from the Composer; this skill refuses there.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("yorik.send_prepared_email")


def _staged(user_id) -> dict:
    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?",
                           (f"pending_email_draft_{user_id}",)).fetchone()
    try:
        return json.loads(row["value"]) if row and row["value"] else {}
    except (TypeError, ValueError):
        return {}


async def execute(ctx, to: str) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("send_prepared_email needs a signed-in user")
    if not str(getattr(ctx, "source", "") or "").startswith("token:"):
        return {"sent": False, "_llm_hint": "REJECTED: in Yorik's chat the email card's own Send button sends. "
                                            "Tell the user to press it."}
    draft = _staged(user_id)
    if not draft.get("to"):
        return {"sent": False, "_llm_hint": "REJECTED: no prepared email is waiting. Prepare it with prepare_email "
                                            "first; nothing was sent."}
    staged_to = str(draft["to"]).strip()
    if staged_to.lower() != (to or "").strip().lower():
        return {"sent": False, "staged_to": staged_to,
                "_llm_hint": (f"REJECTED: the prepared email is for {staged_to}, not {to}. Nothing was sent; "
                              "tell the user which email is waiting.")}
    account_id = draft.get("account_id")
    if not account_id:
        return {"sent": False, "_llm_hint": "REJECTED: the user has several email accounts and none is chosen for "
                                            "this email. Ask which one, then prepare it again with from_email."}

    from backend import email_sender, send_attachments
    files = []
    for a in draft.get("attachments") or []:
        try:
            content, mimetype, filename = await asyncio.to_thread(send_attachments.fetch_bytes, str(user_id),
                                                                  a.get("url", ""))
        except Exception as exc:  # noqa: BLE001
            return {"sent": False, "_llm_hint": f"REJECTED: the attachment {a.get('filename')!r} could not be "
                                                f"read ({exc}). Nothing was sent."}
        files.append({"filename": a.get("filename") or filename, "mimetype": a.get("mimetype") or mimetype,
                      "content": content})
    recipients = [x.strip() for x in staged_to.replace(";", ",").split(",") if x.strip()]
    result = await asyncio.to_thread(email_sender.send, account_id, recipients, draft.get("subject") or "",
                                     draft.get("body") or "", None, None, None, None, None, files or None)
    if not result.get("ok"):
        return {"sent": False, "_llm_hint": f"FAILED: the mail server refused: {result.get('error')}. "
                                            "Nothing was sent; tell the user."}
    from backend.email_routes import _close_pending_draft
    try:
        _close_pending_draft(str(user_id), "sent")
    except Exception:  # noqa: BLE001
        log.warning("sent, but the staged draft could not be cleared", exc_info=True)
    log.info("sent the prepared email for user %s via %s", user_id, getattr(ctx, "source", ""))
    return {"sent": True, "to": staged_to, "subject": draft.get("subject") or "",
            "attachments": [f["filename"] for f in files],
            "_llm_hint": "Sent. Tell the user in one short line that it went out."}
