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
import os
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
          "you answer; if none fits, search more specifically or say you found nothing. "
          "If what they ask about may be written in another language or under another name than searched, "
          "search again with universal_search and `also`.")

CALL_ID = "prefetch_search"

_STOP = set("""
was wann wo woher wohin wie wer wem wen welche welcher welches welchen welchem wieso warum weshalb
hat hast haben hatte hatten gibt gab es ist sind war waren wird werden kann kannst könnte soll sollte
ich du er sie wir ihr mir mich dir dich uns euch mein meine meinen meiner dein deine unser unsere
der die das den dem des ein eine einen einem einer und oder aber auch noch nochmal mal eigentlich
denn doch ja nee nein schon so da dann halt eben bitte gerade grad letztens neulich immer
im in am an auf aus bei mit nach von vor zu zum zur für über um bis seit
nicht nix kein keine is isses zeig such find weißt sagen sag mal man ne nen unserem unseren unser
alles alle mail mails email e-mail whatsapp chat chats nachricht nachrichten papier papiere dokument dokumente
sachen sache ding dinge zeug hallo hi dieses diese dieser geschickt geschrieben bekommen also hab
what when where who which how is are was were the a an of to for in on at my your did does do show find
""".split())


def keywords(message: str) -> str:
    """The words that carry the question — the keyword branch of the
    search wants every word to match, a whole sentence never does."""
    words = re.findall(r"[\wäöüÄÖÜß@.-]+", (message or "").lower())
    kept = [w.strip(".-") for w in words if w.strip(".-") and w.strip(".-") not in _STOP and len(w.strip(".-")) > 1]
    return " ".join(kept[:6]) or (message or "").strip()


def for_model(raw: Dict[str, Any], query: str, per_source: int = 3) -> Dict[str, Any]:
    """The search result as the model sees it: at most `per_source` hits
    per source with short snippets, sources whose titles carry a query
    word first. The whole list overran the 6000-character cut, and the
    recording "Regeln für Yorik" fell off behind twenty mail and
    WhatsApp hits (rerun 2026-09-27)."""
    words = [w for w in re.findall(r"\w+", (query or "").lower()) if len(w) > 3]

    def title_hits(hits: List[Dict[str, Any]]) -> int:
        return sum(1 for h in hits for w in words if w in str(h.get("title") or "").lower())

    results = raw.get("results") or {}
    order = sorted((k for k, v in results.items() if v), key=lambda k: -title_hits(results[k]))
    out: Dict[str, Any] = {}
    for k in order:
        out[k] = [{kk: (str(vv)[:160] if kk == "snippet" else vv)
                   for kk, vv in h.items() if kk != "thumbnail_url" and vv not in (None, "")}
                  for h in results[k][:per_source]]
    shown = sum(len(v) for v in out.values())
    res = {**{k: v for k, v in raw.items() if k != "results"}, "results": out}
    if shown < (raw.get("total") or 0):
        res["more"] = f"{raw['total'] - shown} further hits not shown; search more specifically to see them."
    return res


VARIANTS_PROMPT = (
    "You help a household search find what the user means. The user asked: \"{message}\"\n"
    "The search uses these words: \"{query}\".\n"
    "Give up to 3 other search wordings that would find the same thing if it is written in another "
    "language (often English) or under another name: the company behind a product, a brand or legal "
    "name, the usual word on an invoice or receipt. A few words each, no explanations. "
    "Answer only JSON: {{\"also\": [\"...\"]}}; if nothing makes sense, {{\"also\": []}}.")
VARIANTS_TIMEOUT_S = 4.0


async def variants(message: str, query: str) -> List[str]:
    """Other wordings from the model, before the search: the user's own
    words missed the English invoice and the Anthropic receipt for
    "Claude" (2026-09-27; Dirk chose this over a second search by code).
    Nothing when the model is slow or answers oddly — the search then
    runs with the user's words alone."""
    import httpx
    from .llm import _thinking_kwargs_enabled
    body: Dict[str, Any] = {
        "messages": [{"role": "user", "content": VARIANTS_PROMPT.format(message=message, query=query)}],
        "temperature": 0.2, "max_tokens": 80}
    if os.getenv("HOMEOS_MODEL"):
        body["model"] = os.getenv("HOMEOS_MODEL")
    if _thinking_kwargs_enabled():
        body["chat_template_kwargs"] = {"enable_thinking": False}
        body["reasoning_effort"] = "none"
    base = os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    try:
        async with httpx.AsyncClient(timeout=VARIANTS_TIMEOUT_S) as client:
            r = await client.post(f"{base}/chat/completions", json=body,
                                  headers={"Authorization": "Bearer not-used"})
        r.raise_for_status()
        raw = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        found = re.search(r"\{.*\}", raw, re.S)
        also = json.loads(found.group(0)).get("also") if found else []
    except Exception:  # noqa: BLE001
        return []
    seen = {query.strip().lower()}
    out: List[str] = []
    for v in also if isinstance(also, list) else []:
        v = str(v).strip()[:80]
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out[:3]


def merge(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Several search results as one: the first run's hits first, each
    hit once per source."""
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for run in runs:
        for source, hits in (run.get("results") or {}).items():
            have = merged.setdefault(source, [])
            ids = {h.get("id") for h in have}
            have.extend(h for h in hits if h.get("id") not in ids)
    return {**runs[0], "results": merged, "total": sum(len(v) for v in merged.values())}


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
    import asyncio
    user = {"id": user_id, "role": role or "member"}
    query = keywords(message)
    also = await variants(message, query)
    try:
        runs = await asyncio.gather(*(universal_search(q=q, user=user) for q in [query, *also]))
        raw = merge(list(runs))
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
    body = render_skill_result({**for_model(raw, " ".join([query, *also])), "_llm_hint": HEADER},
                               skill="universal_search")
    args: Dict[str, Any] = {"query": query, **({"also": also} if also else {})}
    call = {"id": CALL_ID, "type": "function", "function": {
        "name": "invoke_skill",
        "arguments": json.dumps({"name": "universal_search", "args": args}, ensure_ascii=False)}}
    messages: List[Dict[str, Any]] = [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": CALL_ID, "name": "invoke_skill", "content": body},
    ]
    return {"messages": messages, "raw": raw, "query": query}
