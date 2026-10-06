"""Noticing a failed turn and offering a report — never sending one.

After every chat turn (backend/agent/loop.py) the thin tool trace of
the answer is looked at: a tool that answered with an error, or a
search that ran and an answer that says "found nothing" although the
question names someone Yorik knows. The person then gets a
notification: "Something went wrong — report it?" leading to the
conversation, where the thumbs-down panel drafts the report. One
suggestion per conversation; nothing when tier 3 is off.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from backend.database import get_conn

from . import enabled

log = logging.getLogger("yorik.diagnostics")

KIND = "diag_suggest"
_ERRORISH = re.compile(r"^\s*(?:\{\s*\"error\"|error\b|fehler\b|exception\b|traceback\b)", re.I)


def classify(messages: List[Dict[str, Any]], user_id: str) -> Optional[str]:
    """'skill_failure' | 'search_empty' | None for the last turn."""
    if not messages or not isinstance(messages[-1], dict) or messages[-1].get("role") != "assistant":
        return None
    answer = messages[-1]
    meta = answer.get("metadata") if isinstance(answer.get("metadata"), dict) else {}
    trace = meta.get("tool_trace") or []
    searched = False
    for e in trace if isinstance(trace, list) else []:
        if not isinstance(e, dict):
            continue
        res = e.get("result")
        if isinstance(res, str) and _ERRORISH.match(res):
            return "skill_failure"
        if str(e.get("name") or "") in ("universal_search", "prefetch_search"):
            searched = True
    text = answer.get("content") if isinstance(answer.get("content"), str) else ""
    if searched and text:
        from .report import NOTHING_FOUND
        if NOTHING_FOUND.search(text):
            question = next((m.get("content") for m in reversed(messages[:-1])
                             if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)), "")
            if question and _names_someone(question, user_id):
                return "search_empty"
    return None


def _names_someone(question: str, user_id: str) -> bool:
    try:
        from .pseudonyms import build_dictionary
        low = question.lower()
        return any(len(v) >= 4 and v.lower() in low for k, v, _ in build_dictionary(user_id) if k in ("person", "org", "email", "group"))
    except Exception:  # noqa: BLE001
        return False


def after_turn(conversation_id: Optional[str], user_id: Any, messages: List[Dict[str, Any]]) -> Optional[str]:
    """Called off the event loop after a turn was saved. Returns the
    trigger it suggested, or None."""
    if not conversation_id or not user_id or not enabled(3):
        return None
    try:
        trigger = classify(messages, str(user_id))
        if not trigger:
            return None
        with get_conn() as conn:
            seen = conn.execute("SELECT 1 FROM notifications WHERE user_id = ? AND kind = ? AND payload_json LIKE ? LIMIT 1",
                                (user_id, KIND, f'%"{conversation_id}"%')).fetchone()
        if seen:
            return None
        from backend import notifications
        notifications.create(user_id=str(user_id), kind=KIND, title="Something went wrong — report it?",
                             body=("A tool failed in this conversation." if trigger == "skill_failure"
                                   else "Yorik found nothing, though the question names someone it knows."),
                             payload={"conversation_id": conversation_id, "trigger": trigger},
                             navigate_to="/r/chat")
        log.info("diagnostics: suggested a report (%s) for a conversation", trigger)
        return trigger
    except Exception as exc:  # noqa: BLE001 — never let this touch the chat
        log.info("diagnostics: detect skipped: %s", exc)
        return None
