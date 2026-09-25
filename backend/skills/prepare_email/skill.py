"""prepare_email — stage an email draft (recipient, subject, body, one
attachment) for the user to review and send themselves in the Email app.

Never calls email_sender.send() and never touches SMTP. Stages the
draft two ways:
  1. Server-side in app_settings (key pending_email_draft_<user_id>),
     read-and-cleared by GET /api/email/pending-draft on the Email
     app's mount. This is the reliable path — it works no matter which
     tab, window, or later moment the user opens Email in, unlike a
     sessionStorage handoff tied to one browser tab. First version of
     this skill used ONLY a `stash_pending_email` UI action into
     sessionStorage; the user reported "I don't see the draft" because
     that action landed in whichever tab happened to receive the
     assistant's reply, not necessarily the tab they later checked
     Email in (2026-09-25).
  2. Still ALSO fires that `stash_pending_email` UI action (consumed
     by EmailStashBridge) for the same-tab case, where it's instant —
     no extra request needed if the user navigates to Email in the
     very tab the reply arrived in.
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

    # 2. Instant same-tab hint, if the user is about to navigate to
    # Email in the very tab this reply lands in.
    from backend.ui_tools import _append
    _append({"type": "stash_pending_email", **payload})

    return {
        "ok":            True,
        "staged_to":     to,
        "attachment_id": row["id"],
        "_llm_hint": (
            f"Staged, NOT sent. Tell the user in one line that the email to {to} is ready and waiting in the Email "
            f"app with '{row['filename']}' attached, and that they need to open Email themselves, check it, and "
            f"press send — nothing was sent or delivered by this call." + account_note
        ),
    }
