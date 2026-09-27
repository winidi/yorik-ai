"""Search everything first — the start page's universal search, run by
code before the model thinks, for a question or lookup.

Chat test 2026-09-26/27: a prompt rule "universal_search first" was not
enough; "was kostet der Kampfsportverein" went to find_known_provider
and the map, "Beates Schulungsprojekt" to the calendar. With the hits
already in front of it the model starts from everything the household
has. Plain commands ("trag ein", "schreib", "erinner mich") and short
replies ("ja", "ok") are left alone.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

_QUESTION = re.compile(
    r"\?|(?:^|[\s,;:])(?:was|wann|wo|woher|wohin|wie|wer|wem|wen|welche[rsnm]?|wieso|warum|weshalb|"
    r"hat|hast|haben|hatte|gibt\s+es|gab\s+es|kannst\s+du\s+mir\s+sagen|weißt\s+du|zeig|such|find|"
    r"what|when|where|who|which|how|show|find|search|did|does|do)\b", re.I)
_COMMAND = re.compile(
    r"^\s*(?:bitte\s+)?(?:schreib|schreibe|trag|erinner|leg|lösch|lösche|mach|füg|verschieb|plan|überweis|"
    r"schick|sende|ruf|antworte|markier|hak|stell|setz|kündig|bestätig|öffne|sag\s+(?!mir\b)\w+|"
    r"write|add|remind|delete|create|send|plan|schedule|move|mark)\b", re.I)

# Agenda questions have their own path with cards (check_calendar /
# check_tasks / plan_my_day); a search in front of them made the model
# answer from hits and drop the task card (e2e 2026-09-27).
_AGENDA = re.compile(
    r"\b(?:heute|morgen|übermorgen|gestern|nächste\s+woche|diese\s+woche|wochenende|montag|dienstag|mittwoch|"
    r"donnerstag|freitag|samstag|sonntag|termin\w*|aufgab\w*|to-?dos?|zu\s+tun|steht\s+\w*\s*an|kalender|"
    r"frei|today|tomorrow|tonight|this\s+week|next\s+week|appointment\w*|tasks?|chores?|calendar)\b", re.I)

HEADER = ("Automatic search across all sources for this question (mail, WhatsApp, documents, calendar, "
          "tasks, contacts, bank, letters, recordings, photos). Open a hit that fits and check it before "
          "you answer; if none fits, search more specifically or say you found nothing.")

CALL_ID = "prefetch_search"

_STOP = set("""
was wann wo woher wohin wie wer wem wen welche welcher welches welchen welchem wieso warum weshalb
hat hast haben hatte hatten gibt gab es ist sind war waren wird werden kann kannst könnte soll sollte
ich du er sie wir ihr mir mich dir dich uns euch mein meine meinen meiner dein deine unser unsere
der die das den dem des ein eine einen einem einer und oder aber auch noch nochmal mal eigentlich
denn doch ja nee nein schon so da dann halt eben bitte gerade grad letztens neulich immer
im in am an auf aus bei mit nach von vor zu zum zur für über um bis seit
nicht nix kein keine is isses zeig such find weißt sagen sag mal man ne nen unserem unseren unser
what when where who which how is are was were the a an of to for in on at my your did does do show find
""".split())


def keywords(message: str) -> str:
    """The words that carry the question — the keyword branch of the
    search wants every word to match, a whole sentence never does."""
    words = re.findall(r"[\wäöüÄÖÜß@.-]+", (message or "").lower())
    kept = [w.strip(".-") for w in words if w.strip(".-") and w.strip(".-") not in _STOP and len(w.strip(".-")) > 1]
    return " ".join(kept[:6]) or (message or "").strip()


def should_search(message: str) -> bool:
    text = (message or "").strip()
    if len(text.split()) < 4 or text.startswith(("Ich habe „", "[")):
        return False
    if _COMMAND.search(text) or _AGENDA.search(text):
        return False
    return bool(_QUESTION.search(text))


async def run(message: str, *, user_id: Any, role: Optional[str]) -> Optional[Dict[str, Any]]:
    """The two messages to put before the model (a synthetic tool call and
    its result) plus the raw hits for source labels, or None."""
    if not user_id or not should_search(message):
        return None
    from backend.skills.registry import get_registry
    skill = get_registry().get("universal_search")
    perms = getattr(skill, "permissions", None) or []
    eff = "admin" if role == "platform_admin" and "admin" in perms else role
    if skill is None or (perms and eff not in perms and "*" not in perms):
        return None
    from backend.search_routes import universal_search
    try:
        query = keywords(message)
        raw = await universal_search(q=query, user={"id": user_id, "role": role or "member"})
    except Exception:  # noqa: BLE001 — a failed prefetch leaves the model to search itself
        return None
    if not raw or not raw.get("total"):
        return None
    # Photo search has no relevance cut-off — it always returns five
    # pictures. Only a question about photos gets them.
    if not re.search(r"\b(foto|fotos|bild|bilder|photo|photos|picture|video|videos|aufnahmen?)\b", message, re.I):
        raw = {**raw, "results": {k: v for k, v in (raw.get("results") or {}).items() if k != "immich"}}
        raw["total"] = sum(len(v) for v in raw["results"].values())
        if not raw["total"]:
            return None
    from backend.ui_tools import render_skill_result
    body = render_skill_result({**raw, "_llm_hint": HEADER}, skill="universal_search")
    call = {"id": CALL_ID, "type": "function", "function": {
        "name": "invoke_skill",
        "arguments": json.dumps({"name": "universal_search", "args": {"query": query}},
                                ensure_ascii=False)}}
    messages: List[Dict[str, Any]] = [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": CALL_ID, "name": "invoke_skill", "content": body},
    ]
    return {"messages": messages, "raw": raw, "query": query}
