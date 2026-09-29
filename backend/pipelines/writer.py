"""The LLM writes the follow-up mails.

Twice per reminder:

* `draft_sequence` — when a pipeline is created: what kind of matter it
  is, what counts as the answer, how many reminders after how many days
  (with a short reason), and a first text for each. The person reviews
  the whole plan up front.
* `rewrite_due` — when a reminder falls due: written again with what
  happened since (how long ago, earlier reminders, mails that came but
  were not the answer, a concrete deadline). The person approves this
  fresh text before it goes.

The model only writes text. Recipients, threading, timing and whether to
send at all stay with the engine. When the model cannot be reached, the
plain template stays and the step says so (`payload.source`).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional

log = logging.getLogger("yorik.pipelines.writer")

TIMEOUT_S = 180


def _complete(prompt: str, max_tokens: int = 1800) -> str:
    """One completion from the local model (same endpoint as the chat)."""
    import httpx
    from ..agent.llm import _thinking_kwargs_enabled

    base = os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": max_tokens,
    }
    model = os.getenv("HOMEOS_MODEL", "")
    if model:
        body["model"] = model
    if _thinking_kwargs_enabled():
        body["chat_template_kwargs"] = {"enable_thinking": False}
        body["reasoning_effort"] = "none"
    r = httpx.post(f"{base}/chat/completions", json=body, timeout=TIMEOUT_S,
                   headers={"Authorization": "Bearer not-used"})
    r.raise_for_status()
    return ((r.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()


def _json(raw: str) -> Optional[dict[str, Any]]:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _fmt(d: Optional[datetime]) -> str:
    return d.astimezone().strftime("%d.%m.%Y") if d else ""


def _sender(owner: str) -> dict[str, str]:
    from ..database import conn_ctx
    with conn_ctx() as c:
        r = c.execute("SELECT name, first_name, last_name, language FROM user_profiles WHERE id = ?",
                      (owner,)).fetchone()
    if not r:
        return {"name": "", "first": ""}
    full = " ".join(p for p in (r["first_name"], r["last_name"]) if p) or r["name"] or ""
    return {"name": full, "first": r["first_name"] or (r["name"] or "").split(" ")[0]}


_RULES = """Rules:
- Write in the language of the original mail, in the voice of the person who sent it, as a perfectly ordinary email.
- Plain text only, no Markdown, no placeholders like [Name] or [Date]. Leave out what you don't know.
- Invent nothing: no numbers, amounts, dates, legal sections or threats that are not in the material.
- Name the matter concretely (what it was about, when it was written, which reference number if there is one), so the mail makes sense without the quote.
- Short: 3 to 7 sentences plus greeting and sign-off. The original message is quoted below it automatically; don't repeat it.
- The subject stays the original mail's subject with "Re: " in front, unless a deadline belongs in it."""


def draft_sequence(owner: str, origin: dict[str, Any]) -> Optional[dict[str, Any]]:
    """{goal, kind, reminders:[{after_days, why, subject, body}], handover_days} or None."""
    who = _sender(owner)
    sent = _fmt(_parse(origin.get("sent_at")))
    prompt = f"""You help a person follow up on an email they sent until the expected answer arrives.
Plan the follow-up reminders and write them.

The original mail (sent on {sent} by {who['name'] or 'the person'} to {', '.join(origin.get('to') or [])}):
Subject: {origin.get('subject') or ''}
---
{(origin.get('body_excerpt') or '')[:3000]}
---

Think about:
1. What kind of matter is this (cancellation, inquiry, claim, application, appointment, other)?
2. What answer is expected? (e.g. "confirmation of the cancellation with the date the contract ends", not merely an acknowledgement of receipt.)
3. How many reminders are appropriate (1 to 3), and after how many days each (counted from the previous message)? Go by the matter and by any deadlines in the mail. An authority or company usually takes longer than a private person; a claim can take firmer intervals.
4. How many days after the last reminder should the person take over?
5. Each reminder is a little firmer than the one before: first a friendly follow-up, then a request for an answer within a deadline.

{_RULES}
- When the reminders actually go out is not fixed yet; Yorik rewrites each one shortly before sending, with the real dates. So name NO date other than the original mail's: "my reminder" rather than "my reminder of …", "within seven days" rather than "by …".
- No invented urgency ("the deadline is running out") unless it is in the mail.
- Sign with "{who['name']}".

Answer ONLY with JSON in exactly this form:
{{"kind": "cancellation|inquiry|claim|application|appointment|other", "goal": "the expected answer in one short sentence",
  "reminders": [{{"after_days": 5, "why": "one short sentence on why this interval", "subject": "...", "body": "..."}}],
  "handover_days": 7}}"""
    try:
        data = _json(_complete(prompt))
    except Exception as exc:  # noqa: BLE001
        log.warning("draft_sequence: model not reachable: %s", exc)
        return None
    if not data:
        log.warning("draft_sequence: no usable JSON from the model")
        return None
    reminders = []
    for r in (data.get("reminders") or [])[:3]:
        body = str(r.get("body") or "").strip()
        if not body:
            continue
        reminders.append({
            "after_days": max(1, min(60, int(r.get("after_days") or 5))),
            "why": str(r.get("why") or "").strip()[:200],
            "subject": str(r.get("subject") or "").strip()[:300],
            "body": body[:6000],
        })
    if not reminders:
        return None
    return {
        "kind": str(data.get("kind") or "").strip()[:40],
        "goal": str(data.get("goal") or "").strip()[:300],
        "reminders": reminders,
        "handover_days": max(1, min(60, int(data.get("handover_days") or 7))),
    }


def rewrite_due(p: dict[str, Any], step: dict[str, Any], history: dict[str, Any]) -> Optional[dict[str, str]]:
    """A fresh text for the reminder that is due now. {subject, body} or None.

    history: {sent: [{date, body}], not_answers: [{date, from, subject, snippet}],
              number, total}"""
    who = _sender(p["owner_user_id"])
    origin = p["origin"]
    now = datetime.now().astimezone()
    sent_at = _parse(origin.get("sent_at"))
    days = (now - sent_at).days if sent_at else None
    deadline = _fmt(now + timedelta(days=7))
    lines = [f"- on {s['date']}: {s['body'][:600]}" for s in history.get("sent") or []]
    notes = [f"- on {n['date']} from {n['from']}: \"{n['subject']}\" — {n.get('snippet') or ''}"[:300]
             for n in history.get("not_answers") or []]
    last = history.get("number") == history.get("total")
    prompt = f"""Write the reminder that is to go out today ({_fmt(now)}).

What it is about: {p.get('goal') or ''}
Original mail of {_fmt(sent_at)}{f' ({days} days ago)' if days is not None else ''} to {', '.join(origin.get('to') or [])}:
Subject: {origin.get('subject') or ''}
---
{(origin.get('body_excerpt') or '')[:2500]}
---

Reminders sent so far:
{chr(10).join(lines) or '- none, this is the first'}

Mails that came since but were NOT the expected answer (the person said so):
{chr(10).join(notes) or '- none'}

This is reminder {history.get('number')} of {history.get('total')}.{' It is the last one: ask clearly but politely for an answer by ' + deadline + '.' if last else ''}
If an acknowledgement of receipt came, mention it ("you confirmed receipt on …, but a reply on the matter is still outstanding").
If reminders have already gone out, refer to them and don't repeat the same sentences.
Previous draft for orientation (may be rewritten freely):
{(step['payload'].get('body') or '')[:1500]}

{_RULES}
- Sign with "{who['name']}".

Answer ONLY with JSON: {{"subject": "...", "body": "..."}}"""
    try:
        data = _json(_complete(prompt, max_tokens=900))
    except Exception as exc:  # noqa: BLE001
        log.warning("rewrite_due: model not reachable: %s", exc)
        return None
    if not data or not str(data.get("body") or "").strip():
        return None
    return {"subject": str(data.get("subject") or step["payload"].get("subject") or "").strip()[:300],
            "body": str(data["body"]).strip()[:6000]}


def _parse(value: Any) -> Optional[datetime]:
    from .store import to_dt
    return to_dt(value)
