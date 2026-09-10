"""Reports on top of a recording's transcript.

One LLM pass over the turns with a template that fixes the sections:
decisions, tasks (with a person), highlights, friction, open questions,
dates. The result is stored in recordings.report_json and survives the
audio's deletion. Tasks are proposals: nothing is created until someone
adopts them (chat → add_task; the app → its Adopt button).

The call goes straight to the household LLM endpoint (no tool loop).
Structured output is requested as a tool call because the local
servers have no json_schema mode; when the model answers in prose the
JSON is fished out of the text instead. Thinking is on for this one
step (HOMEOS_REPORT_REASONING, default medium): an hour of talk with
attributions is where it pays.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .database import get_conn

log = logging.getLogger("yorik.recording_reports")

MAX_TRANSCRIPT_CHARS = int(os.getenv("HOMEOS_REPORT_MAX_CHARS", "90000"))   # ~20k tokens of German
REASONING = (os.getenv("HOMEOS_REPORT_REASONING") or "medium").strip().lower()
TIMEOUT_S = float(os.getenv("HOMEOS_REPORT_TIMEOUT") or 600)
AUTO_REPORT_KINDS = tuple(k for k in (os.getenv("HOMEOS_RECORDING_AUTO_REPORT") or "dinner,meeting").split(",") if k)

TEMPLATES: Dict[str, Dict[str, str]] = {
    "dinner": {
        "name": "Dinner",
        "focus": (
            "This is a family or household dinner conversation. Households want to remember what was "
            "agreed, who took on what, the nice moments worth keeping, and what did not go well so it "
            "can be talked about calmly later. Keep the warmth; do not judge people."
        ),
    },
    "meeting": {
        "name": "Meeting",
        "focus": (
            "This is a meeting. Decisions and who does what by when matter most; highlights and "
            "friction are secondary but still listed when present."
        ),
    },
}

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Two or three sentences on what the conversation was about."},
        "decisions": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "who": {"type": "string"}, "when": {"type": "string"}},
            "required": ["text"]}},
        "tasks": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Imperative, short, e.g. 'Call the school about the trip'."},
            "person": {"type": "string", "description": "Name of the participant who took it on, or the speaker label; empty if nobody did."},
            "due_date": {"type": "string", "description": "YYYY-MM-DD when a day was named or implied, else empty."},
            "why": {"type": "string", "description": "One line quoting or paraphrasing where it came from."}},
            "required": ["title"]}},
        "highlights": {"type": "array", "items": {"type": "string"}, "description": "Nice moments, praise, successes, plans people were happy about."},
        "friction": {"type": "array", "items": {"type": "string"}, "description": "What did not go well, annoyances, conflicts, things left unresolved between people."},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "dates": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD or empty"},
            "time": {"type": "string", "description": "HH:MM or empty"}},
            "required": ["text"]}},
    },
    "required": ["summary", "decisions", "tasks", "highlights", "friction", "open_questions", "dates"],
}


def _prompt(template: str, transcript: str, participants: List[str], recorded_on: str) -> List[Dict[str, str]]:
    t = TEMPLATES.get(template) or TEMPLATES["meeting"]
    names = ", ".join(participants) if participants else "unknown"
    system = (
        "You turn a transcript of a spoken conversation into a short structured report. "
        f"{t['focus']} "
        "Write every text in the language the people spoke. Use only what is in the transcript; "
        "never invent decisions, tasks or dates. Anything one person will do is a task (title, person, "
        "due_date); decisions are agreements that are not one person's to-do. Attribute by the speaker "
        "label as given; replace a label like 'Sprecher 2' by a participant's name only when the "
        "transcript itself says who that is (addressed by name), never by guessing. Resolve relative days "
        f"(tomorrow, Saturday) against the recording date {recorded_on}. Keep each item to one line and "
        "paraphrase instead of quoting; no quotation marks inside texts. "
        "Answer by calling write_report with the JSON; no other text."
    )
    user = f"Participants: {names}.\nRecording date: {recorded_on}.\n\nTranscript:\n{transcript}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _repair_json(text: str) -> str:
    """Escape stray double quotes inside strings. Models quoting the
    transcript („Ich mache das" – …) break strict JSON; a quote that is
    not followed by , } ] : (or the end) cannot be a closing one."""
    out, in_str, i, n = [], False, 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                out.append(text[i:i + 2]); i += 2; continue
            if ch == '"':
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j >= n or text[j] in ",}]:":
                    in_str = False
                else:
                    out.append('\\"'); i += 1; continue
        elif ch == '"':
            in_str = True
        out.append(ch); i += 1
    return "".join(out)


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(_repair_json(text))


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = _loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _llm_json(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """One request; tool call preferred, JSON in prose accepted."""
    import httpx
    from .agent import llm as _agent_llm
    base = os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    model = os.getenv("HOMEOS_MODEL", "qwen3.5-9b")
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 12000,
        "tools": [{"type": "function", "function": {
            "name": "write_report", "description": "Store the structured report.", "parameters": SCHEMA}}],
        "tool_choice": "auto",
    }
    if _agent_llm._thinking_kwargs_enabled():
        on = REASONING not in ("", "none", "off")
        body["chat_template_kwargs"] = {"enable_thinking": on}
        body["reasoning_effort"] = REASONING if on else "none"
    with httpx.Client(timeout=TIMEOUT_S) as c:
        r = c.post(f"{base}/chat/completions", json=body, headers={"Authorization": "Bearer not-used"})
    if r.status_code != 200:
        raise RuntimeError(f"LLM error {r.status_code}: {r.text[:200]}")
    msg = (r.json().get("choices") or [{}])[0].get("message") or {}
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        if fn.get("name") == "write_report":
            args = fn.get("arguments")
            if isinstance(args, str):
                args = _extract_json(args)
            if isinstance(args, dict):
                return args
    data = _extract_json(msg.get("content") or "")
    if not data:
        raise RuntimeError("LLM returned no report JSON")
    return data


def _unnest(v: Any) -> Any:
    """Qwen sometimes hands a list back as a JSON string inside the tool
    arguments ('tasks': '[{...}]'); parse those, leave the rest alone."""
    if isinstance(v, str):
        t = v.strip()
        if t[:1] in ("[", "{"):
            try:
                return _loads(t)
            except ValueError:
                log.warning("recording_reports: could not parse nested section: %s", t[:120])
                return v
    return v


def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
    data = {k: _unnest(v) for k, v in (data or {}).items()}

    def strs(xs) -> List[str]:
        return [str(x).strip() for x in (xs or []) if str(x).strip()] if isinstance(xs, list) else []

    def objs(xs, keys) -> List[Dict[str, str]]:
        out = []
        for x in (xs or []) if isinstance(xs, list) else []:
            if isinstance(x, str):
                x = {keys[0]: x}
            if not isinstance(x, dict) or not str(x.get(keys[0], "")).strip():
                continue
            out.append({k: str(x.get(k) or "").strip() for k in keys})
        return out

    tasks = objs(data.get("tasks"), ["title", "person", "due_date", "why"])
    for t in tasks:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", t["due_date"] or ""):
            t["due_date"] = ""
    dates = objs(data.get("dates"), ["text", "date", "time"])
    for d in dates:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d["date"] or ""):
            d["date"] = ""
    return {
        "summary": str(data.get("summary") or "").strip(),
        "decisions": objs(data.get("decisions"), ["text", "who", "when"]),
        "tasks": tasks,
        "highlights": strs(data.get("highlights")),
        "friction": strs(data.get("friction")),
        "open_questions": strs(data.get("open_questions")),
        "dates": dates,
    }


def build_report(rid: int, template: Optional[str] = None, *, notify: bool = True) -> Dict[str, Any]:
    """Generate, store and (optionally) announce the report. Blocking."""
    from . import recordings as R
    row = R._row(rid)
    if not row:
        raise KeyError(rid)
    if row["status"] != "done":
        raise ValueError(f"transcript is not ready (status {row['status']})")
    template = template if template in TEMPLATES else (row["kind"] if row["kind"] in TEMPLATES else "meeting")
    transcript = R.transcript_text(rid, max_chars=MAX_TRANSCRIPT_CHARS)
    if not transcript.strip():
        raise ValueError("the transcript is empty")
    names = R._names(R._participants(row) + [str(row["owner_user_id"])])
    recorded_on = (row.get("started_at") or date.today().isoformat())[:10]
    data = _clean(_llm_json(_prompt(template, transcript, list(names.values()), recorded_on)))
    data["template"] = template
    data["recording_id"] = rid
    data["generated_at"] = R._now()
    R._set(rid, report_json=json.dumps(data, ensure_ascii=False), report_template=template, report_at=data["generated_at"])
    log.info("recording_reports: #%s %s report — %d tasks, %d decisions, %d highlights, %d friction",
             rid, template, len(data["tasks"]), len(data["decisions"]), len(data["highlights"]), len(data["friction"]))
    if notify:
        notify_report(rid, row, data)
    return data


def get_report(rid: int) -> Optional[Dict[str, Any]]:
    from . import recordings as R
    row = R._row(rid)
    if not row or not row.get("report_json"):
        return None
    try:
        return json.loads(row["report_json"])
    except ValueError:
        return None


def notify_report(rid: int, row: Dict[str, Any], data: Dict[str, Any]) -> None:
    from . import notifications as _notif
    from . import recordings as R
    title = row["title"] or TEMPLATES.get(data.get("template") or "", {}).get("name", "Recording")
    bits = []
    if data["tasks"]:
        bits.append(f"{len(data['tasks'])} task{'s' if len(data['tasks']) != 1 else ''}")
    if data["decisions"]:
        bits.append(f"{len(data['decisions'])} decision{'s' if len(data['decisions']) != 1 else ''}")
    if data["highlights"]:
        bits.append(f"{len(data['highlights'])} nice moment{'s' if len(data['highlights']) != 1 else ''}")
    body = ", ".join(bits) or data["summary"][:120]
    say = quote(f"Show me the report of recording {rid}.")
    for uid in [str(row["owner_user_id"])] + R._participants(row):
        try:
            _notif.create(user_id=uid, kind="recording_report", title=f"{title}: report ready", body=body,
                          payload={"recording_id": rid}, navigate_to=f"/r/chat?say={say}")
        except Exception as exc:  # noqa: BLE001
            log.warning("recording_reports: notify %s failed: %s", uid, exc)
