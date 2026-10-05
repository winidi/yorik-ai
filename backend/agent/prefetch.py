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

import asyncio
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
erinnerst erinnere erinnern erinnert zeigen zeigst wurde wurden worden vom ging gegangen gesagt sagt kam kommt
kommen steht stehen stand lies lesen drin drinnen darin darum dazu davon dabei letzte letzten letztes letzer
letzes zuletzt neueste neuesten aktuelle aktuellen irgendwer irgendwas irgendwo irgendwann jemand dass muss
musst müssen jetzt nich nicht wollte wollten will willst habe hatte gibts gabs neues neue neuer schon wieder
wegen bzw bisschen bissl heut heute gestern vorgestern woche monat jahr tag tage mir mal eben finde finden
suche suchen gesucht geschaut angesehen kannst könntest würdest wo was wie wer wen wem lief läuft eigentlich
siehst sieht sehen seht gesehen schau schaust guck guckst müsste müssten sollte sollten bald demnächst
remember remind showed show tell told sent got get give last latest recent recently someone something
anything anyone wanted want need needs must should could would please again yesterday week month year
""".split())


def keywords(message: str) -> str:
    """The words that carry the question — the keyword branch of the
    search wants every word to match, a whole sentence never does."""
    words = re.findall(r"[\wäöüÄÖÜß@.-]+", (message or "").lower())
    kept = [w.strip(".-") for w in words if w.strip(".-") and w.strip(".-") not in _STOP and len(w.strip(".-")) > 1]
    return " ".join(kept[:6]) or (message or "").strip()


_MONTHS = {m: i + 1 for i, m in enumerate(
    "januar februar märz april mai juni juli august september oktober november dezember".split())}
_MONTHS.update({m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())})
_MONTHS.update({"jan": 1, "feb": 2, "mär": 3, "mrz": 3, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
                "sep": 9, "sept": 9, "okt": 10, "oct": 10, "nov": 11, "dez": 12, "dec": 12})
_DAY = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{2,4})?(?!\d)")
_MONTH_WORD = re.compile(r"\b(?:im|in|vom|von|seit|ab|anfang|ende|mitte)?\s*(" + "|".join(sorted(_MONTHS, key=len, reverse=True))
                         + r")\b(?:\s+(\d{4}))?", re.I)
_RECENT = re.compile(r"\b(?:letzte[nrs]?|letzes|neueste[nrs]?|aktuelle[nrs]?|zuletzt|jüngste[nrs]?|"
                     r"last|latest|most\s+recent|newest)\b", re.I)
def _days(t, n: int):
    from datetime import timedelta
    return t - timedelta(days=n)


def _last_month(t):
    from datetime import date, timedelta
    end = date(t.year, t.month, 1) - timedelta(days=1)
    return date(end.year, end.month, 1), end


_RELATIVE = [
    (re.compile(r"\bvorgestern\b|\bday before yesterday\b", re.I), lambda t, n: (_days(t, 2), _days(t, 2))),
    (re.compile(r"\bgestern\b|\byesterday\b", re.I), lambda t, n: (_days(t, 1), _days(t, 1))),
    (re.compile(r"\b(?:letzte|vergangene|vorige)\s+woche\b|\blast\s+week\b", re.I),
     lambda t, n: (_days(t, 7 + t.weekday()), _days(t, 1 + t.weekday()))),
    (re.compile(r"\b(?:letzten|vergangenen|vorigen)\s+monat\b|\blast\s+month\b", re.I), lambda t, n: _last_month(t)),
    (re.compile(r"\b(?:letzte[ns]?|vergangene[ns]?)\s+(\d+)\s+tage[n]?\b|\blast\s+(\d+)\s+days\b", re.I),
     lambda t, n: (_days(t, int(n or 7)), t)),
]


def time_scope(message: str, today=None) -> Dict[str, Any]:
    """What the question says about *when*, as search filters: a day or
    month ("die Überweisung vom 2.7.", "im September"), a relative span
    ("gestern", "letzte Woche"), and whether the newest matching rows are
    wanted ("die letzten Mails von Beate"). {} when nothing is said.
    Relative spans need no parser library: the household asks in a
    handful of ways (search test set 2026-10-05)."""
    from datetime import date, timedelta
    t = today or date.today()
    today = t
    text = message or ""
    out: Dict[str, Any] = {}
    m = _DAY.search(text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
        try:
            day = date(year, mo, d)
            # A day that has not come yet is what the message is about
            # ("nen Termin am 1.11. geschickt"), not when it was sent.
            if day <= today + timedelta(days=1):
                out["date_from"] = out["date_to"] = day.isoformat()
        except ValueError:
            pass
    if "date_from" not in out:
        for rx, span in _RELATIVE:
            mm = rx.search(text)
            if mm:
                n = next((g for g in mm.groups() if g), None)
                a, b = span(t, n)
                out["date_from"], out["date_to"] = a.isoformat(), b.isoformat()
                break
    if "date_from" not in out:
        mm = _MONTH_WORD.search(text)
        if mm and mm.group(1).lower() in _MONTHS and (mm.group(2) or re.search(
                r"\b(?:im|vom|von|seit|ab|anfang|ende|mitte)\s+" + re.escape(mm.group(1)), text, re.I)):
            mo = _MONTHS[mm.group(1).lower()]
            year = int(mm.group(2)) if mm.group(2) else (today.year if mo <= today.month else today.year - 1)
            first = date(year, mo, 1)
            nxt = date(year + (mo == 12), mo % 12 + 1, 1)
            out["date_from"], out["date_to"] = first.isoformat(), (nxt - timedelta(days=1)).isoformat()
    if _RECENT.search(text):
        out["recent"] = True
    return out


PER_SOURCE_FOR_MODEL = 5


def for_model(raw: Dict[str, Any], query: str, per_source: int = PER_SOURCE_FOR_MODEL) -> Dict[str, Any]:
    """The search result as the model sees it: at most `per_source` hits
    per source with short snippets, sources whose titles carry a query
    word first, then the source with the strongest hit. The whole list
    overran the 6000-character cut, and the recording "Regeln für Yorik"
    fell off behind twenty mail and WhatsApp hits (rerun 2026-09-27).
    Five per source since 2026-10-05: with three, a hit on place 4 of
    its source was lost for good (30 of 100 test questions)."""
    words = [w for w in re.findall(r"\w+", (query or "").lower()) if len(w) > 3]

    def title_hits(hits: List[Dict[str, Any]]) -> int:
        return sum(1 for h in hits for w in words if w in str(h.get("title") or "").lower())

    def best(hits: List[Dict[str, Any]]) -> float:
        return max((float(h.get("_score") or 0) for h in hits), default=0.0)

    results = raw.get("results") or {}
    if raw.get("reranked"):
        order = [k for k, v in results.items() if v]       # the reranker's order of sources
    else:
        order = sorted((k for k, v in results.items() if v), key=lambda k: (-title_hits(results[k]), -best(results[k])))
    out: Dict[str, Any] = {}
    for k in order:
        out[k] = [{kk: (str(vv)[:160] if kk == "snippet" else vv)
                   for kk, vv in h.items()
                   if kk != "thumbnail_url" and not kk.startswith("_") and vv not in (None, "")}
                  for h in results[k][:per_source]]
    shown = sum(len(v) for v in out.values())
    res = {**{k: v for k, v in raw.items() if k not in ("results", "reranked")}, "results": out}
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


async def variants(message: str, query: str, llm: Any = None) -> List[str]:
    """Other wordings from the model, before the search: the user's own
    words missed the English invoice and the Anthropic receipt for
    "Claude" (2026-09-27; Dirk chose this over a second search by code).
    Nothing when the model is slow or answers oddly — the search then
    runs with the user's words alone. Asks the chat's own model server
    (`llm`: address, model and key as set in Settings → LLM); the
    environment is only the fallback."""
    import httpx
    from .llm import _thinking_kwargs_enabled
    body: Dict[str, Any] = {
        "messages": [{"role": "user", "content": VARIANTS_PROMPT.format(message=message, query=query)}],
        "temperature": 0.2, "max_tokens": 80}
    model = getattr(llm, "model", None) or os.getenv("HOMEOS_MODEL")
    if model:
        body["model"] = model
    if _thinking_kwargs_enabled():
        body["chat_template_kwargs"] = {"enable_thinking": False}
        body["reasoning_effort"] = "none"
    base = (getattr(llm, "base_url", None) or os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1")).rstrip("/")
    key = getattr(llm, "api_key", None) or "not-used"
    import time
    from backend import speed
    limit = speed.budget(VARIANTS_TIMEOUT_S, "llm")   # slower machines get longer
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=limit) as client:
            r = await client.post(f"{base}/chat/completions", json=body,
                                  headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        speed.record("llm", time.perf_counter() - t0)
        raw = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        found = re.search(r"\{.*\}", raw, re.S)
        also = json.loads(found.group(0)).get("also") if found else []
    except httpx.TimeoutException:
        speed.record("llm", limit)                      # a timeout is a measurement too
        return []
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


RRF_K = 20
VARIANT_WEIGHT = 0.9       # a hit for the model's other wording counts a little less than one for the user's words
EXTRA_VOTE = 0.001         # further runs' votes for the same hit only break ties


def _twin_key(h: Dict[str, Any]) -> Any:
    """Hits that are the same thing again, across runs: a mail by sender
    and subject (reply prefixes, numbers off), a booking by party, amount
    and purpose, a WhatsApp line by chat and text."""
    src = h.get("source")
    if src == "email":
        # numbers stay in the subject: order numbers tell two orders apart
        subj = re.sub(r"^(?:(?:re|aw|wg|fwd?|fw|antw|erinnerung|reminder)\s*:\s*)+", "", str(h.get("title") or "").lower())
        subj = re.sub(r"\s+", " ", subj).strip()
        body = re.sub(r"\W+", " ", str(h.get("snippet") or "").lower())[:60]
        return (src, str(h.get("subtitle") or "").lower(), subj, body) if subj else None
    if src == "bank":
        return (src, str(h.get("title") or "").lower(), str(h.get("subtitle") or "").split("·")[-1].strip(),
                re.sub(r"\d+", "#", str(h.get("snippet") or "").lower())[:40])
    if src == "whatsapp":
        text = re.sub(r"\s+", " ", str(h.get("snippet") or "").lower())
        return (src, h.get("chat_jid"), text[:80]) if len(text) > 20 else None
    return None


def merge(runs: List[Dict[str, Any]], weights: Optional[List[float]] = None) -> Dict[str, Any]:
    """Several search results as one: every run votes for its hits by
    rank (reciprocal rank fusion, the user's own words weighing most),
    each hit once per source, twins folded into `duplicates`. Until
    2026-10-05 the first run's hits simply came first, so a variant's
    exact hit sat behind five weak ones."""
    weights = weights or [1.0] + [VARIANT_WEIGHT] * (len(runs) - 1)
    best: Dict[str, Dict[Any, float]] = {}
    total: Dict[str, Dict[Any, float]] = {}
    rows: Dict[str, Dict[Any, Dict[str, Any]]] = {}
    for run, weight in zip(runs, weights):
        for source, hits in (run.get("results") or {}).items():
            b, t, r = best.setdefault(source, {}), total.setdefault(source, {}), rows.setdefault(source, {})
            for rank, h in enumerate(hits, 1):
                k = h.get("id")
                r.setdefault(k, h)
                vote = weight / (RRF_K + rank)
                b[k] = max(b.get(k, 0.0), vote)
                t[k] = t.get(k, 0.0) + vote
    # The best single placement counts, further votes only break ties: a
    # plain sum let three paraphrases agreeing on a newsletter outvote
    # the one run that had the right mail first (GoHighLevel, AfD Peine
    # cases, 2026-10-05). A variant's first hit lands about fourth.
    score: Dict[str, Dict[Any, float]] = {
        src: {k: b[k] + EXTRA_VOTE * (total[src][k] - b[k]) for k in b} for src, b in best.items()}
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for source, s in score.items():
        out: List[Dict[str, Any]] = []
        seen: Dict[Any, Dict[str, Any]] = {}
        for k in sorted(s, key=lambda x: -s[x]):
            h = {**rows[source][k], "_score": round(s[k], 4)}
            tk = _twin_key(h)
            if tk is not None and tk in seen:
                seen[tk]["duplicates"] = seen[tk].get("duplicates", 0) + 1 + h.get("duplicates", 0)
                continue
            if tk is not None:
                seen[tk] = h
            out.append(h)
        merged[source] = out
    return {**runs[0], "results": merged, "total": sum(len(v) for v in merged.values())}


async def prepare(message: str, user: Dict[str, Any]) -> tuple[str, Dict[str, str]]:
    """The search words of a question, typos in names corrected against
    the person's own senders and chats ({typo: word}, empty when none)."""
    import asyncio
    from backend import search_vocab
    query = keywords(message)
    fixes = await asyncio.to_thread(search_vocab.correct, query.split(), user.get("id"))
    return search_vocab.apply(query, fixes), fixes


async def search(message: str, query: str, also: List[str], user: Dict[str, Any]) -> Dict[str, Any]:
    """The chat's search for one question: the user's words and the
    model's other wordings, each across every source, the whole question
    for the meaning branch, the question's time scope as filter; one
    merged result. The search test set (search-eval/eval.py) calls this
    too, so what is measured is what the chat does."""
    import asyncio
    from backend.search_routes import universal_search
    from backend import search_rerank
    scope = time_scope(message)
    runs = await asyncio.gather(
        universal_search(q=query, meaning=message, user=user, **scope),
        *(universal_search(q=v, user=user, **scope) for v in also))
    merged = merge(list(runs))
    return await asyncio.to_thread(search_rerank.rerank, message, merged)


def should_search(message: str) -> bool:
    text = (message or "").strip()
    if len(text.split()) < 4 or text.startswith(("Ich habe „", "[")):
        return False
    if _COMMAND.search(text) or _AGENDA.search(text):
        return False
    return bool(_QUESTION.search(text))


_FOLLOW_UP = re.compile(
    r"\b(?:da|davon|dazu|dafür|dabei|damit|die|das|der|den|dem|es|noch|neue[rsn]?|wieder|dann|also|eine|einer|"
    r"it|that|this|those|one|another|again|still)\b", re.I)


def with_context(message: str, previous: Optional[str]) -> tuple[str, Optional[str]]:
    """The search words of a question, with the previous question's words
    when this one leans on it: "Müsste da nicht eine neue ankommen bald?"
    after "Siehst du meine snooze Bestellung?" searched for "müsste
    ankommen bald" and found nothing (Dirk, 2026-10-05). Returns (query,
    the words carried over or None)."""
    own = keywords(message)
    if not previous:
        return own, None
    short = len((message or "").split()) <= 9
    if not (short and _FOLLOW_UP.search(message or "")) and len(own.split()) >= 2:
        return own, None
    carried = [w for w in keywords(previous).split() if w not in own.split()]
    if not carried:
        return own, None
    return " ".join([*own.split(), *carried][:8]), " ".join(carried)


async def run(message: str, *, user_id: Any, role: Optional[str], llm: Any = None,
              previous: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The two messages to put before the model (a synthetic tool call and
    its result) plus the raw hits for source labels, or None. `previous`
    is the user's message before this one, for follow-ups."""
    if not user_id or not should_search(message):
        return None
    from backend.skills.registry import get_registry
    skill = get_registry().get("universal_search")
    perms = getattr(skill, "permissions", None) or []
    eff = "admin" if role == "platform_admin" and "admin" in perms else role
    if skill is None or (perms and eff not in perms and "*" not in perms):
        return None
    user = {"id": user_id, "role": role or "member"}
    try:
        query, fixes = await prepare(message, user)
        query, carried = with_context(message, previous) if previous else (query, None)
        if carried:
            from backend import search_vocab
            fixes = await asyncio.to_thread(search_vocab.correct, query.split(), user_id)
            query = search_vocab.apply(query, fixes)
            message_for_meaning = f"{previous.strip()} {message.strip()}"
        else:
            message_for_meaning = message
        also = await variants(message_for_meaning, query, llm)
        raw = await search(message_for_meaning, query, also, user)
    except Exception:  # noqa: BLE001 — a failed prefetch leaves the model to search itself
        return None
    if carried:
        raw["with_previous_question"] = carried
    if fixes:
        raw["corrected"] = fixes        # "rivertie" → "riverty": the chat can say so
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
    args: Dict[str, Any] = {"query": query, **({"also": also} if also else {}), **({"corrected": fixes} if fixes else {}),
                            **({"with_previous_question": carried} if carried else {})}
    call = {"id": CALL_ID, "type": "function", "function": {
        "name": "invoke_skill",
        "arguments": json.dumps({"name": "universal_search", "args": args}, ensure_ascii=False)}}
    messages: List[Dict[str, Any]] = [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": CALL_ID, "name": "invoke_skill", "content": body},
    ]
    return {"messages": messages, "raw": raw, "query": query}
