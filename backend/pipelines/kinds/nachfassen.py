"""Pipeline kind `nachfassen`: a mail went out, an answer is expected;
remind when none comes, hand over to the person after the last step.

Stage 1: the check reads mail only and never decides "fulfilled" by
itself. Every mail that might be the answer is shown to the person
("vielleicht"); telling an acknowledgement of receipt from the actual
confirmation is stage 2 (docs/plans/2026-09-25-pipelines.md).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from email.utils import make_msgid
from typing import Any, Optional

from .. import store
from ..sources import mail as mail_src

KIND = "nachfassen"
LABEL = "Antwort verfolgen"

DEFAULT_DAYS = (5, 7, 7)   # reminder, second reminder, hand-over
SEARCH_SLACK = timedelta(minutes=5)


def _fmt_date(value: Any) -> str:
    d = store.to_dt(value)
    if not d:
        return ""
    return d.astimezone().strftime("%d.%m.%Y")


def _reply_subject(subject: str) -> str:
    s = (subject or "").strip()
    return s if re.match(r"(?i)^(re|aw|antw)\s*:", s) else f"Re: {s}"


def _sender_name(owner: str) -> str:
    from ...database import conn_ctx
    with conn_ctx() as c:
        r = c.execute("SELECT name, first_name, last_name FROM user_profiles WHERE id = ?",
                      (owner,)).fetchone()
    if not r:
        return ""
    full = " ".join(p for p in (r["first_name"], r["last_name"]) if p)
    return full or r["name"] or ""


def origin_from_mail(mail: dict[str, Any]) -> dict[str, Any]:
    try:
        refs = json.loads(mail.get("references_ids") or "[]") or []
    except ValueError:
        refs = []
    try:
        to = [a.get("email") for a in json.loads(mail.get("to_addrs") or "[]") if a.get("email")]
    except (ValueError, AttributeError):
        to = []
    try:
        cc = [a.get("email") for a in json.loads(mail.get("cc_addrs") or "[]") if a.get("email")]
    except (ValueError, AttributeError):
        cc = []
    return {
        "mail_id": int(mail["id"]),
        "account_id": int(mail["account_id"]),
        "account_email": mail.get("account_email"),
        "message_id": mail.get("message_id"),
        "references": refs,
        "subject": mail.get("subject") or "",
        "to": to,
        "cc": cc,
        "sent_at": mail.get("date_received") or mail.get("date_sent"),
        "body_excerpt": (mail.get("body_text") or "")[:4000],
    }


def default_steps(origin: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    name = _sender_name(owner)
    when = _fmt_date(origin.get("sent_at"))
    subject = _reply_subject(origin.get("subject") or "")
    to = list(origin.get("to") or [])
    sig = f"\n\nFreundliche Grüße\n{name}" if name else "\n\nFreundliche Grüße"
    first = (
        "Guten Tag,\n\n"
        f"ich komme zurück auf meine Nachricht vom {when}. Bisher habe ich keine Antwort erhalten. "
        "Könnten Sie mir bitte kurz bestätigen, dass sie angekommen ist, und mir sagen, "
        "wie es weitergeht?" + sig
    )
    second = (
        "Guten Tag,\n\n"
        f"auf meine Nachricht vom {when} und meine Erinnerung habe ich leider noch keine Antwort. "
        "Bitte melden Sie sich innerhalb der nächsten sieben Tage." + sig
    )
    return [
        {"action": "mail_senden", "after_days": DEFAULT_DAYS[0],
         "payload": {"to": to, "subject": subject, "body": first}},
        {"action": "mail_senden", "after_days": DEFAULT_DAYS[1],
         "payload": {"to": to, "subject": subject, "body": second}},
        {"action": "uebergabe", "after_days": DEFAULT_DAYS[2], "payload": {}},
    ]


def default_config() -> dict[str, Any]:
    # Families write on weekends too; "werktags" is a choice per pipeline.
    return {"send_days": "alle", "send_from_hour": 8, "send_to_hour": 20}


def quote(origin: dict[str, Any]) -> str:
    when = _fmt_date(origin.get("sent_at"))
    who = origin.get("account_email") or ""
    lines = (origin.get("body_excerpt") or "").strip().splitlines()[:60]
    quoted = "\n".join(f"> {ln}" for ln in lines)
    return f"\n\n\nAm {when} schrieb {who}:\n{quoted}" if quoted else ""


# ─── check ──────────────────────────────────────────────────────────

def check(p: dict[str, Any]) -> dict[str, Any]:
    """{result, candidates, took_over, sources, problems, summary}.

    result: 'vielleicht' | 'kann_nicht_pruefen' | 'sicher_nicht'
    ('erfuellt' only ever comes from the person in stage 1)."""
    owner = p["owner_user_id"]
    since = store.to_dt(p["since_at"])
    origin = p["origin"]
    feats = p["features"]
    own_ids = [i for i in [origin.get("message_id")] + store.own_message_ids(p["id"]) if i]

    fresh = mail_src.freshness(owner, since)
    # Mail servers stamp arrival to the second and their clocks drift; an
    # instant auto-reply can carry a time just before our own send. Look a
    # little further back rather than miss it.
    cands = mail_src.search(owner, since - SEARCH_SLACK, feats, own_ids,
                            exclude_ids=feats.get("dismissed_mail_ids") or [])
    took_over = [r for r in mail_src.own_replies(owner, since, feats, own_ids, origin.get("message_id"))
                 if r["id"] not in (feats.get("dismissed_sent_ids") or [])]
    searched = mail_src.count_since(owner, since)

    if cands:
        result = "vielleicht"
    elif not fresh["ok"]:
        result = "kann_nicht_pruefen"
    else:
        result = "sicher_nicht"
    n_acc = len(fresh["accounts"])
    summary = (f"{n_acc} Postfach" + ("" if n_acc == 1 else "er") +
               f" inkl. Spam, {searched} Mails seit {_fmt_date(since)} durchsucht")
    return {
        "result": result,
        "candidates": cands,
        "took_over": took_over,
        "sources": {"mail": fresh},
        "problems": fresh["problems"],
        "searched": searched,
        "summary": summary,
    }


# ─── act ────────────────────────────────────────────────────────────

def perform(p: dict[str, Any], step: dict[str, Any], attempt: int) -> dict[str, Any]:
    """Send one reminder. {ok, status: gesendet|fehler|schon_versucht, error?}."""
    from ... import email_sender

    origin = p["origin"]
    payload = step["payload"]
    to = [a for a in payload.get("to") or [] if a]
    if not to:
        return {"ok": False, "status": "fehler", "error": "kein Empfänger"}
    account_email = origin.get("account_email") or ""
    domain = account_email.split("@", 1)[1] if "@" in account_email else "yorik.local"
    message_id = make_msgid(domain=domain).strip("<>")
    key = f"p{p['id']}:s{step['id']}:v{attempt}"
    if not store.action_begin(p["id"], step["id"], key, "mail", message_id):
        return {"ok": False, "status": "schon_versucht", "error": "dieser Versand wurde schon begonnen"}

    earlier = store.own_message_ids(p["id"])
    refs = [r for r in (origin.get("references") or []) + [origin.get("message_id")] + earlier
            if r and r != message_id]
    body = (payload.get("body") or "") + quote(origin)
    try:
        res = email_sender.send(
            account_id=int(origin["account_id"]), to=to, subject=payload.get("subject") or "",
            body_text=body, in_reply_to=origin.get("message_id"), references=refs,
            message_id=message_id,
        )
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if res.get("ok"):
        store.action_finish(key, "gesendet")
        return {"ok": True, "status": "gesendet", "message_id": message_id}
    store.action_finish(key, "fehler", str(res.get("error") or "")[:500])
    return {"ok": False, "status": "fehler", "error": res.get("error")}
