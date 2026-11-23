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


# A press on Send after the email already went out (the agent sent it in
# the same turn, or a second press) found no draft, and the agent prepared
# and sent the same email again (seen live 2026-11-23). What went out last
# is kept for a while, so the answer is "already sent" instead.
ALREADY_SENT_MINUTES = 30
# The same email (recipient, subject, text) prepared again and sent within
# this time is the same press twice, not a new email: the agent prepared it
# anew because it no longer saw that it went out.
SAME_EMAIL_MINUTES = 10


def fingerprint(to: str, subject: str, body: str) -> str:
    import hashlib
    text = "\n".join([(to or "").strip().lower(), (subject or "").strip(), " ".join((body or "").split())])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _last_sent_key(user_id) -> str:
    return f"last_sent_email_{user_id}"


def _remember_sent(user_id, to: str, subject: str, card_uid: str, body: str = "") -> None:
    import datetime as dt
    from backend.database import get_conn
    value = json.dumps({"to": to, "subject": subject, "card_uid": card_uid,
                        "fingerprint": fingerprint(to, subject, body),
                        "at": dt.datetime.now(dt.timezone.utc).isoformat()})
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                     (_last_sent_key(user_id), value))
        conn.commit()


def _recently_sent(user_id, to: str, minutes: int = ALREADY_SENT_MINUTES) -> dict:
    """The email that went out last, when it went to `to` within `minutes`."""
    import datetime as dt
    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_last_sent_key(user_id),)).fetchone()
    try:
        last = json.loads(row["value"]) if row and row["value"] else {}
        at = dt.datetime.fromisoformat(last["at"])
    except (TypeError, ValueError, KeyError):
        return {}
    if str(last.get("to") or "").strip().lower() != (to or "").strip().lower():
        return {}
    if dt.datetime.now(dt.timezone.utc) - at > dt.timedelta(minutes=minutes):
        return {}
    return last


def _sent_card(to: str, subject: str, card_uid: str) -> None:
    """Tells the client that the prepared email's card is done: it shows
    "sent" there instead of its Send button (Brepper: cards.py)."""
    from backend.ui_tools import _append
    _append({"type": "email_sent", "to": to, "subject": subject, "card_uid": card_uid})


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
        last = _recently_sent(user_id, to)
        if last:
            _sent_card(last["to"], last.get("subject") or "", last.get("card_uid") or "")
            return {"sent": False, "already_sent": True, "to": last["to"], "subject": last.get("subject") or "",
                    "_llm_hint": ("ALREADY SENT: this email went out already, nothing was sent again. Tell the user "
                                  "in one short line that it was already sent; do not prepare or send it again.")}
        return {"sent": False, "_llm_hint": ("REJECTED: no prepared email is waiting, nothing was sent. Do not "
                                             "prepare and send one on your own: tell the user, and prepare a new "
                                             "one only when they ask for it.")}
    staged_to = str(draft["to"]).strip()
    if staged_to.lower() != (to or "").strip().lower():
        return {"sent": False, "staged_to": staged_to,
                "_llm_hint": (f"REJECTED: the prepared email is for {staged_to}, not {to}. Nothing was sent; "
                              "tell the user which email is waiting.")}
    last = _recently_sent(user_id, staged_to, SAME_EMAIL_MINUTES)
    if last and last.get("fingerprint") == fingerprint(staged_to, draft.get("subject") or "", draft.get("body") or ""):
        from backend.email_routes import _close_pending_draft
        try:
            _close_pending_draft(str(user_id), "sent")
        except Exception:  # noqa: BLE001
            log.warning("could not clear the repeated draft", exc_info=True)
        _sent_card(staged_to, draft.get("subject") or "", str((draft.get("card") or {}).get("uid") or ""))
        return {"sent": False, "already_sent": True, "to": staged_to, "subject": draft.get("subject") or "",
                "_llm_hint": ("ALREADY SENT: this same email went out a few minutes ago, nothing was sent again. "
                              "Tell the user in one short line; if they really want it twice, they say so and "
                              "you send it after those minutes.")}
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
    card_uid = str((draft.get("card") or {}).get("uid") or "")
    try:
        _remember_sent(user_id, staged_to, draft.get("subject") or "", card_uid, draft.get("body") or "")
    except Exception:  # noqa: BLE001
        log.warning("sent, but could not remember it", exc_info=True)
    _sent_card(staged_to, draft.get("subject") or "", card_uid)
    log.info("sent the prepared email for user %s via %s", user_id, getattr(ctx, "source", ""))
    return {"sent": True, "to": staged_to, "subject": draft.get("subject") or "",
            "attachments": [f["filename"] for f in files],
            "_llm_hint": "Sent. Tell the user in one short line that it went out."}
