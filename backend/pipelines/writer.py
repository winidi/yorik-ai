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


_RULES = """Regeln:
- Schreibe in der Sprache der ursprünglichen Mail (meist Deutsch), in der Stimme der absendenden Person, als ganz normale E-Mail.
- Nur Klartext, keine Markdown-Zeichen, keine Platzhalter wie [Name] oder [Datum]. Was du nicht weißt, lässt du weg.
- Erfinde nichts: keine Nummern, Beträge, Daten, Paragraphen oder Drohungen, die nicht aus dem Material stammen.
- Nenne das Anliegen konkret (worum es ging, wann geschrieben, welche Nummer, falls vorhanden), damit man die Mail ohne das Zitat versteht.
- Kurz: 3 bis 7 Sätze plus Anrede und Gruß. Das ursprüngliche Schreiben wird automatisch darunter zitiert; wiederhole es nicht.
- Der Betreff bleibt der Betreff der ursprünglichen Mail mit „Re: “ davor, außer eine Frist gehört hinein."""


def draft_sequence(owner: str, origin: dict[str, Any]) -> Optional[dict[str, Any]]:
    """{goal, kind, reminders:[{after_days, why, subject, body}], handover_days} or None."""
    who = _sender(owner)
    sent = _fmt(_parse(origin.get("sent_at")))
    prompt = f"""Du hilfst einer Person, an einer gesendeten E-Mail dranzubleiben, bis die erwartete Antwort kommt.
Plane die Nachfass-Erinnerungen und schreibe sie.

Die ursprüngliche Mail (gesendet am {sent} von {who['name'] or 'der Person'} an {', '.join(origin.get('to') or [])}):
Betreff: {origin.get('subject') or ''}
---
{(origin.get('body_excerpt') or '')[:3000]}
---

Überlege:
1. Was für ein Anliegen ist das (kuendigung, anfrage, forderung, bewerbung, termin, sonstiges)?
2. Welche Antwort wird erwartet? (z. B. „Kündigungsbestätigung mit Datum des Vertragsendes“, nicht bloß eine Eingangsbestätigung.)
3. Wie viele Erinnerungen sind angemessen (1 bis 3) und nach wie vielen Tagen jeweils (gezählt ab der vorigen Nachricht)? Richte dich nach dem Anliegen und nach Fristen, die in der Mail stehen. Eine Behörde oder Firma braucht meist länger als eine Privatperson; eine Forderung verträgt festere Abstände.
4. Nach wie vielen Tagen nach der letzten Erinnerung soll die Person selbst übernehmen?
5. Jede Erinnerung wird etwas bestimmter als die vorige: erst freundlich nachfragen, dann um Antwort innerhalb einer Frist bitten.

{_RULES}
- Wann die Erinnerungen tatsächlich rausgehen, steht noch nicht fest; Yorik schreibt jede kurz vor dem Senden mit den echten Daten neu. Nenne deshalb außer dem Datum der ursprünglichen Mail KEIN Datum: „meine Erinnerung“ statt „meine Erinnerung vom …“, „innerhalb von sieben Tagen“ statt „bis zum …“.
- Keine erfundene Dringlichkeit („die Frist läuft ab“), wenn sie nicht in der Mail steht.
- Unterschreibe mit „{who['name']}“.

Antworte NUR mit JSON in genau dieser Form:
{{"kind": "...", "goal": "erwartete Antwort in einem kurzen Satz",
  "reminders": [{{"after_days": 5, "why": "ein kurzer Satz, warum dieser Abstand", "subject": "...", "body": "..."}}],
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
    lines = [f"- am {s['date']}: {s['body'][:600]}" for s in history.get("sent") or []]
    notes = [f"- am {n['date']} von {n['from']}: „{n['subject']}“ — {n.get('snippet') or ''}"[:300]
             for n in history.get("not_answers") or []]
    last = history.get("number") == history.get("total")
    prompt = f"""Schreibe die Erinnerung, die heute ({_fmt(now)}) rausgehen soll.

Worum es geht: {p.get('goal') or ''}
Ursprüngliche Mail vom {_fmt(sent_at)}{f' (vor {days} Tagen)' if days is not None else ''} an {', '.join(origin.get('to') or [])}:
Betreff: {origin.get('subject') or ''}
---
{(origin.get('body_excerpt') or '')[:2500]}
---

Bisher gesendete Erinnerungen:
{chr(10).join(lines) or '- keine, das ist die erste'}

Mails, die seitdem kamen, aber NICHT die erwartete Antwort waren (von der Person so eingestuft):
{chr(10).join(notes) or '- keine'}

Das ist Erinnerung {history.get('number')} von {history.get('total')}.{' Es ist die letzte: bitte deutlich, aber höflich um Antwort bis zum ' + deadline + '.' if last else ''}
Wenn eine Eingangsbestätigung kam, erwähne sie („Sie haben den Eingang am … bestätigt, eine inhaltliche Antwort steht aber noch aus“).
Wenn schon Erinnerungen rausgingen, beziehe dich darauf und wiederhole nicht dieselben Sätze.
Vorheriger Entwurf zur Orientierung (darf frei umgeschrieben werden):
{(step['payload'].get('body') or '')[:1500]}

{_RULES}
- Unterschreibe mit „{who['name']}“.

Antworte NUR mit JSON: {{"subject": "...", "body": "..."}}"""
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
