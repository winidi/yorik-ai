"""prepare_email — stage an email draft (recipient, subject, body, one
attachment) for the user to review and send themselves in the Email app.

Never calls email_sender.send() and never touches SMTP. Two things
happen:
  1. The draft is written server-side into app_settings (key
     pending_email_draft_<user_id>), read-and-cleared by GET
     /api/email/pending-draft on the Email app's mount. This is the
     data channel — it works no matter which tab, window, or later
     moment the user opens Email in. (V1 used only a sessionStorage
     handoff; the user reported "I don't see the draft" because that
     only reached the one browser tab that happened to receive the
     assistant's reply — 2026-09-25.)
  2. An `email_ready` UI action is returned so the chat renders a real
     card — recipient, subject, a body preview, the attachment name —
     with an "Open and send" button (EmailDraftReadyCard.tsx) that
     jumps straight to /email. Before this, the assistant only ever
     said "open Email yourself" in plain text, which the user rightly
     called out as not UX-friendly (2026-09-25) next to how a letter
     or invoice draft gets a proper inline card elsewhere in Yorik.
Nothing is sent, filed, or delivered by this skill itself.
"""

from __future__ import annotations

import json
from typing import Any, Optional


async def execute(
    ctx,
    to: str,
    subject: str,
    body: str,
    attachment_id: int,
    from_email: Optional[str] = None,
) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("prepare_email needs a signed-in user")

    to = (to or "").strip()
    if not to or "@" not in to:
        return {"ok": False, "_llm_hint": f"REJECTED: {to!r} doesn't look like an email address. "
                                          f"Ask the user for a real recipient."}

    from backend import chat_attachments as A
    row = A.get(int(attachment_id), str(user_id))
    if not row:
        return {"ok": False, "_llm_hint": "REJECTED: there is no attachment with this number for this user. "
                                          "Ask them to attach or create the file again."}

    account_id: Optional[int] = None
    account_note = ""
    if from_email:
        from backend import email_sender
        acct = email_sender.resolve_account_for_from_address(from_email)
        if acct and str(acct.get("owner_user_id")) == str(user_id):
            account_id = acct["id"]
        else:
            account_note = (
                f" Couldn't find {from_email!r} among the user's own configured email accounts — the Composer's "
                f"default account will be pre-selected instead; mention that so they can double-check it."
            )

    payload = {
        "to":          to,
        "subject":     (subject or "").strip(),
        "body":        body or "",
        "account_id":  account_id,
        "attachments": [{
            "url":      f"/api/chat/attachments/{row['id']}/raw",
            "filename": row["filename"],
            "mimetype": row["mime_type"] or "application/octet-stream",
        }],
    }

    # 1. Durable — survives a different tab, a reload, or the user
    # coming back later. See module docstring for why this exists.
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (f"pending_email_draft_{user_id}", json.dumps(payload)),
        )
        conn.commit()

    # 2. A real card in the chat, not just a sentence — mirrors
    # writing_draft_created's WritingDraftCard for letters/invoices.
    body_preview = (body or "").strip().replace("\n", " ")
    if len(body_preview) > 160:
        body_preview = body_preview[:157] + "…"
    from backend.ui_tools import _append
    _append({
        "type":                "email_ready",
        "to":                  to,
        "subject":             payload["subject"],
        "preview":             body_preview,
        "attachment_filename": row["filename"],
    })

    return {
        "ok":            True,
        "staged_to":     to,
        "attachment_id": row["id"],
        "_llm_hint": (
            f"Staged, NOT sent — a card with an 'open and send' button already showed the user everything "
            f"(recipient, subject, preview, attachment). Just confirm in one short line that it's ready; don't "
            f"repeat the details the card already shows, and don't say 'open the Email app yourself', the card "
            f"does that." + account_note
        ),
    }
