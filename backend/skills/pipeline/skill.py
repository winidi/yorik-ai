"""pipeline — the Pipelines app from the chat: follow up a sent mail,
list what Yorik is following, pause or resume one.

Dirk (2026-09-27): after a mail went out, "fass da nach, wenn keine
Antwort kommt" in the same chat should start the follow-up. The
pipeline is created as a draft, Yorik writes the reminders, and the
person approves each reminder on the pipeline page before it starts —
the rule for the Pipelines app (every outgoing mail is approved).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

LOOKBACK_DAYS = 14


async def execute(ctx, op: str = "list", to: Optional[str] = None, subject: Optional[str] = None,
                  mail_id: Optional[int] = None, pipeline_id: Optional[int] = None) -> Dict[str, Any]:
    from backend.skills.registry import require_user_id
    owner = str(require_user_id(ctx))
    op = (op or "list").strip().lower()
    if op in ("follow_up", "nachfassen", "create"):
        return await _follow_up(owner, to, subject, mail_id)
    if op == "list":
        return _list(owner)
    if op in ("pause", "resume"):
        return _set_running(owner, pipeline_id, op)
    raise ValueError("op must be follow_up, list, pause or resume")


def _find_sent_mail(owner: str, to: Optional[str], subject: Optional[str]) -> Optional[Dict[str, Any]]:
    from backend.database import get_conn
    where = ["owner_user_id = ?", "COALESCE(is_sent, 0) = 1", "COALESCE(is_draft, 0) = 0",
             "COALESCE(date_sent, date_received) >= ?"]
    from datetime import datetime, timedelta, timezone
    params: list[Any] = [owner, (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()]
    if to:
        where.append("LOWER(to_addrs) LIKE ?")
        params.append(f"%{to.strip().lower()}%")
    if subject:
        where.append("LOWER(COALESCE(subject, '')) LIKE ?")
        params.append(f"%{subject.strip().lower()}%")
    with get_conn() as conn:
        r = conn.execute(
            "SELECT id, subject, to_addrs, COALESCE(date_sent, date_received) AS sent FROM email_messages "
            f"WHERE {' AND '.join(where)} ORDER BY COALESCE(date_sent, date_received) DESC LIMIT 1", params,
        ).fetchone()
    return dict(r) if r else None


def _staged_only(owner: str, to: Optional[str]) -> bool:
    """A mail prepared in the chat but not sent yet (prepare_email's draft)."""
    from backend.database import get_conn
    with get_conn() as conn:
        r = conn.execute("SELECT value FROM app_settings WHERE key = ?", (f"pending_email_draft_{owner}",)).fetchone()
    if not r:
        return False
    try:
        staged = json.loads(r["value"] or "{}")
    except ValueError:
        return False
    return not to or to.strip().lower() in str(staged.get("to", "")).lower()


async def _follow_up(owner: str, to: Optional[str], subject: Optional[str], mail_id: Optional[int]) -> Dict[str, Any]:
    from backend.pipelines import routes, store
    mail = {"id": int(mail_id)} if mail_id else _find_sent_mail(owner, to, subject)
    if not mail:
        why = ("The mail was prepared in the chat but not sent yet — it has to be sent first (the card's "
               "\"Öffnen und senden\"), then follow-up can start."
               if _staged_only(owner, to) else
               f"No mail sent in the last {LOOKBACK_DAYS} days matches"
               + (f" to {to}" if to else "") + (f" about {subject}" if subject else "") + ".")
        return {"ok": False, "_llm_hint": why + " Tell the person in one short sentence."}
    try:
        pid = routes.create_follow_up(owner, int(mail["id"]))
    except routes.CreateError as exc:
        return {"ok": False, "_llm_hint": f"Could not start follow-up: {exc}. Tell the person."}
    asyncio.get_running_loop().run_in_executor(None, routes.draft_with_llm, pid, owner)
    p = store.get(pid, owner) or {}
    link = f"/r/pipelines/{pid}"
    from backend.ui_tools import _append
    _append({"type": "pipeline_ready", "pipeline_id": pid, "title": p.get("title") or "",
             "to": (p.get("origin") or {}).get("to") or [], "link": link})
    return {"ok": True, "pipeline_id": pid, "title": p.get("title"), "link": link,
            "_llm_hint": ("shown_to_user: a card links the new follow-up. Yorik is writing the reminders now; "
                          "the person approves each reminder on that page and starts it — nothing is sent "
                          "before. Say this in one or two short sentences.")}


def _list(owner: str) -> Dict[str, Any]:
    from backend.pipelines import engine, store
    rows = []
    for p in store.list_for(owner):
        steps = store.steps(p["id"])
        nxt = engine.next_open_step(steps)
        row = {"pipeline_id": p["id"], "title": p["title"], "state": STATE_EN.get(p["state"], p["state"]),
               "mode": {"begleitet": "accompanied", "autonom": "autonomous"}.get(p["mode"], p["mode"]),
               "attention": p.get("attention"), "next_step": nxt["action"] if nxt else None,
               "link": f"/r/pipelines/{p['id']}"}
        reply = _reply_received(owner, p.get("origin") or {})
        if reply:
            row["reply_received"] = reply
            # "next step: send mail" read as "your answer is still unsent"
            # (2026-09-28); with an answer in, nothing is next.
            row["next_step"] = None
        rows.append(row)
    return {"pipelines": rows,
            "_llm_hint": ("States: draft = not started yet; running; paused; done; cancelled. "
                          "Answer from this list only. "
                          # approved by Dirk 2026-09-28
                          "If reply_received is set, say that an answer came — from whom and when; "
                          "nothing is left to follow up.")}


# Stored state codes → the words the model sees.
STATE_EN = {"entwurf": "draft", "laeuft": "running", "pausiert": "paused",
            "erledigt": "done", "abgebrochen": "cancelled"}


def _reply_received(owner: str, origin: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The first mail from someone else in the followed mail's conversation
    after it was sent. GoHighLevel: Oliver had answered, the draft
    pipeline still read as open (2026-09-27)."""
    from backend.database import get_conn
    from backend.search_routes import _local
    mail_id, message_id = origin.get("mail_id"), origin.get("message_id")
    if not mail_id and not message_id:
        return None
    with get_conn() as conn:
        own = conn.execute("SELECT thread_id, date_received FROM email_messages WHERE id = ? AND owner_user_id = ?",
                           (int(mail_id or 0), owner)).fetchone()
        thread_id = own["thread_id"] if own else None
        since = origin.get("sent_at") or (own["date_received"] if own else "") or ""
        r = conn.execute(
            "SELECT id, from_name, from_email, subject, date_received FROM email_messages "
            "WHERE owner_user_id = ? AND COALESCE(is_sent, 0) = 0 AND COALESCE(is_draft, 0) = 0 "
            "AND (in_reply_to = ? OR (? <> '' AND thread_id = ?)) AND date_received > ? "
            "ORDER BY date_received LIMIT 1",
            (owner, message_id or "", thread_id or "", thread_id or "", since),
        ).fetchone()
    if not r:
        return None
    return {"from": r["from_name"] or r["from_email"], "date": _local(r["date_received"]),
            "subject": r["subject"] or "", "mail_id": int(r["id"])}


def _set_running(owner: str, pipeline_id: Optional[int], op: str) -> Dict[str, Any]:
    from backend.pipelines import store
    if not pipeline_id:
        return {"ok": False, "_llm_hint": "Which pipeline? Call pipeline with op=list first and pass pipeline_id."}
    p = store.get(int(pipeline_id), owner)
    if not p:
        return {"ok": False, "_llm_hint": f"Pipeline {pipeline_id} not found for this person."}
    from backend.messages import tr
    want_from, want_to = ("laeuft", "pausiert") if op == "pause" else ("pausiert", "laeuft")
    note = tr("pipelines.event.paused" if op == "pause" else "pipelines.event.resumed", user_id=owner) + " (Chat)"
    if op == "resume" and not store.person_enabled(owner):
        return {"ok": False, "_llm_hint": "Pipelines are switched off for this person. Tell them."}
    if p["state"] != want_from:
        return {"ok": False, "_llm_hint": f"Pipeline is {STATE_EN.get(p['state'], p['state'])}, cannot {op}. Tell the person."}
    store.update(p["id"], state=want_to, next_run_at=None if op == "pause" else store.now())
    store.event(p["id"], "mensch", note)
    return {"ok": True, "pipeline_id": p["id"], "state": STATE_EN[want_to],
            "_llm_hint": f"Pipeline '{p['title']}' is now {STATE_EN[want_to]}. Confirm in one short sentence."}
