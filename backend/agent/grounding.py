"""Grounding check — hard values and quotes in an answer must come from
what Yorik actually looked up.

Chat test 2026-09-26/27: the model twice gave Mama's "account number"
(DE44 …, 1234567890) without calling a single tool. A rule in the
prompt did not stop it. This check runs in code before an answer is
shown or read out:

* hard values — IBANs, phone numbers, mail addresses, money amounts —
  must appear in a tool result of the conversation or in what the user
  typed;
* quotes — lines starting with ">" — must appear word for word (case,
  spacing and quote marks aside; "…" joins pieces of one source).

What is found gets a source chip built here, not by the model (label
and link from the raw skill result). What is not found sends the answer
back once; after that the person gets an honest "nicht belegt".
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ─── patterns ────────────────────────────────────────────────────────

IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
PHONE = re.compile(r"(?<![\w+])(?:\+\d{2}|00\d{2}|0)\d[\d /-]{6,}\d(?![\w])")
MAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_NUM = r"\d{1,3}(?:[.  ]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?"
MONEY = re.compile(rf"(?:(?:€|EUR)\s?(?P<a>{_NUM}))|(?:(?P<b>{_NUM})\s?(?:€|EUR\b|Euro\b|euro\b))")
LONGNUM = re.compile(r"(?<![\w.,])\d(?:[ ]?\d){6,}(?![\w.,]*\d)")
APPROX = re.compile(r"(?:ca\.|circa|rund|etwa|ungefähr|knapp|gut|über|unter|fast|~|≈)\s*$", re.I)

# While streaming: once the text so far could hold one of the above,
# the rest is held back until the check has run.
HOLD = re.compile(r"(?m)\b[A-Z]{2}\d{2}(?:\b|\s?\d)|^\s*>|\d\s?(?:€|EUR|Euro)|€\s?\d|@[\w-]+\.|(?:\+|00)\d{2}\s?\d|\b0\d{3,}[ /-]?\d")


@dataclass
class Verdict:
    ok: bool
    missing: List[str] = field(default_factory=list)
    sources: List[Dict[str, str]] = field(default_factory=list)
    checked: int = 0


def needs_hold(text_so_far: str) -> bool:
    return bool(HOLD.search(text_so_far or ""))


# ─── normalising ─────────────────────────────────────────────────────

def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _phone_key(s: str) -> str:
    d = _digits(s)
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("49"):
        d = d[2:]
    return d.lstrip("0")


def _cents(raw: str) -> Optional[int]:
    """'1.234,50' / '1234.50' / '551,07' / '38' → cents."""
    s = raw.replace(" ", "").replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    elif re.search(r"\.\d{3}$", s) and s.count(".") >= 1 and not re.search(r"\.\d{1,2}$", s):
        s = s.replace(".", "")
    try:
        return round(abs(float(s)) * 100)
    except ValueError:
        return None


def _numbers_in(text: str) -> set:
    """Every number in the evidence, read both the German and the English way."""
    out = set()
    for m in re.finditer(r"-?\d+(?:[.,]\d+)*", text):
        tok = m.group(0).lstrip("-")
        for variant in (tok, tok.replace(".", "#").replace(",", ".").replace("#", ",")):
            c = _cents(variant)
            if c is not None:
                out.add(c)
        try:
            out.add(round(abs(float(tok)) * 100))
        except ValueError:
            pass
    return out


_QUOTE_CHARS = dict.fromkeys(map(ord, "„“”\"«»‚‘’'`´"), None)


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\\n", " ").replace("\\'", "'").replace('\\"', '"')
    s = s.translate(_QUOTE_CHARS)
    s = re.sub(r"[^\w\s]", " ", s.lower())       # punctuation never decides a quote
    return re.sub(r"\s+", " ", s).strip()


# ─── evidence ────────────────────────────────────────────────────────

def _texts(messages: Iterable[Dict[str, Any]]) -> List[str]:
    """What tools returned and what the user typed in this conversation."""
    out = []
    for m in messages:
        if m.get("internal"):
            continue            # the check's own nudge names the unverified values
        if m.get("role") in ("tool", "user"):
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            if c:
                out.append(str(c))
    return out


def _dump(raw: Any) -> str:
    try:
        return json.dumps(raw, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(raw)


# ─── answer parts ────────────────────────────────────────────────────

def _plain(answer: str) -> str:
    """Markdown emphasis out, so "rund **75 €**" still counts as rounded."""
    return re.sub(r"(\*\*|__|\*|`)", "", answer or "")


def _values(answer: str) -> List[Tuple[str, str]]:
    answer = _plain(answer)
    found: List[Tuple[str, str]] = []
    for m in IBAN.finditer(answer):
        found.append(("iban", m.group(0)))
    body = IBAN.sub(" ", answer)
    for m in MAIL.finditer(body):
        found.append(("mail", m.group(0)))
    body = MAIL.sub(" ", body)
    for m in MONEY.finditer(body):
        if APPROX.search(body[max(0, m.start() - 14):m.start()]):
            continue
        found.append(("money", m.group("a") or m.group("b")))
    body = MONEY.sub(" ", body)
    for m in PHONE.finditer(body):
        if len(_digits(m.group(0))) >= 8:
            found.append(("phone", m.group(0)))
    body = PHONE.sub(" ", body)
    # account, customer or order numbers: any run of 7+ digits that is
    # not a date or a year ("1234567890" was given as Mama's account)
    for m in LONGNUM.finditer(body):
        found.append(("number", m.group(0)))
    return found


def _quotes(answer: str) -> List[str]:
    out = []
    for line in answer.splitlines():
        s = line.strip()
        if not s.startswith(">"):
            continue
        s = s.lstrip("> ").strip()
        if re.match(r"(?i)(übersetzung|translation)\s*:", s):
            continue
        # „…“ (bezahlt mit Mastercard): the model's own words after the
        # closing mark are not part of the quote (rerun 2026-09-27).
        inner = re.match(r"^[„“\"«‚]\s*(.+?)\s*[“”\"»‘]\s*(?:\(.*\)|[–—-].*)?[\s“”\"»‘]*$", s)
        if inner:
            s = inner.group(1)
        if len(re.findall(r"\w+", s)) >= 2:
            out.append(s)
    return out


# ─── source labels ───────────────────────────────────────────────────

_SOURCE_NAMES = {"email": "Mail", "whatsapp": "WhatsApp", "paperless": "Dokument", "immich": "Foto",
                 "calendar": "Kalender", "tasks": "Aufgabe", "contacts": "Kontakt", "recordings": "Aufnahme",
                 "drafts": "Entwurf", "bank": "Konto", "letters": "Schreiben", "pipelines": "Nachfassen"}


def _walk(obj: Any, ancestors: List[Dict[str, Any]]):
    if isinstance(obj, dict):
        chain = ancestors + [obj]
        for v in obj.values():
            if isinstance(v, str):
                yield v, chain
            else:
                yield from _walk(v, chain)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, ancestors)


def _label(skill: str, chain: List[Dict[str, Any]]) -> Dict[str, str]:
    inner = chain[-1] if chain else {}

    def pick(*keys: str) -> str:
        for d in reversed(chain):
            for k in keys:
                if d.get(k) not in (None, ""):
                    return str(d[k])
        return ""

    src = pick("source")
    when = pick("when", "date_received", "date", "booking_date", "timestamp", "doc_date", "created_at")
    if when.isdigit():
        when = ""
    elif re.match(r"\d{4}-\d{2}-\d{2}", when):
        when = when[:16].replace("T", " ")
    else:
        when = when[:24]
    if skill == "whatsapp_read" or src == "whatsapp":
        chat = pick("chat", "title", "chat_name")
        jid = pick("chat_jid")
        link = f"/r/whatsapp?chat={jid}" if jid else pick("navigate_to")
        # who wrote it, on the chip: "DE85 … von Mama" was the user's own
        # message (live test 2026-09-27) — the chip makes that visible
        who = str(inner.get("who") or inner.get("subtitle") or "")
        by = "von dir" if who.lower() == "ich" else (f"von {who}" if who and who != chat else "")
        return {"label": " · ".join(x for x in ("WhatsApp", chat, by, when) if x), "link": link}
    if skill == "read_email" or src == "email":
        subject = pick("subject", "title")
        sender = pick("from_name", "from_email", "subtitle")
        mid = pick("message_id", "id") if skill == "read_email" else ""
        link = pick("navigate_to") or (f"/r/email?msg={mid}" if mid else "/r/email")
        return {"label": " · ".join(x for x in ("Mail", sender, subject, when) if x), "link": link}
    if skill in ("read_document", "read_document_vision") or src == "paperless":
        title = pick("title")
        doc = pick("doc_id")
        link = pick("navigate_to") or (f"/r/documents?doc={doc}&source=paperless" if doc else "/r/documents")
        return {"label": " · ".join(x for x in ("Dokument", title) if x), "link": link}
    if skill in ("show_transactions", "spending_summary") or src == "bank":
        cp = pick("counterparty", "title")
        return {"label": " · ".join(x for x in ("Konto", cp, pick("booking_date")) if x), "link": "/r/finance"}
    if skill in ("recording_status", "recording_report") or src == "recordings":
        return {"label": " · ".join(x for x in ("Aufnahme", pick("title")) if x),
                "link": pick("navigate_to") or "/r/recordings"}
    if skill == "calculate":
        return {"label": "Berechnet: " + pick("expression"), "link": ""}
    name = _SOURCE_NAMES.get(src, skill or "Quelle")
    return {"label": " · ".join(x for x in (name, pick("title", "display_name", "subject")) if x),
            "link": pick("navigate_to")}


def _find_source(raws: List[Tuple[str, Any]], match) -> Optional[Dict[str, str]]:
    """The innermost dict of a raw skill result whose text matches."""
    for skill, raw in reversed(raws):
        best = None
        for text, chain in _walk(raw, []):
            if match(text):
                if best is None or len(chain) > len(best):
                    best = chain
        if best is not None:
            return _label(skill, best)
    return None


# ─── the check ───────────────────────────────────────────────────────

def check(answer: str, messages: List[Dict[str, Any]], raws: Optional[List[Tuple[str, Any]]] = None) -> Verdict:
    raws = raws or []
    evidence = _texts(messages) + [_dump(r) for _, r in raws]
    blob = "\n".join(evidence)
    blob_compact = re.sub(r"\s", "", blob).upper()
    digits_blob = _digits(blob)
    numbers = None
    norm_items = None
    missing: List[str] = []
    sources: List[Dict[str, str]] = []
    checked = 0

    def add_source(src: Optional[Dict[str, str]]) -> None:
        if src and src.get("label") and src not in sources:
            sources.append(src)

    for kind, raw in _values(answer):
        checked += 1
        if kind == "iban":
            key = re.sub(r"\s", "", raw).upper()
            ok = key in blob_compact
            src = _find_source(raws, lambda t: key in re.sub(r"\s", "", t).upper()) if ok else None
        elif kind == "mail":
            ok = raw.lower() in blob.lower()
            src = _find_source(raws, lambda t: raw.lower() in t.lower()) if ok else None
        elif kind == "number":
            key = _digits(raw)
            ok = key in digits_blob
            src = _find_source(raws, lambda t: key in _digits(t)) if ok else None
        elif kind == "phone":
            key = _phone_key(raw)
            ok = bool(key) and key in digits_blob
            src = _find_source(raws, lambda t: key and key in _digits(t)) if ok else None
        else:
            if numbers is None:
                numbers = _numbers_in(blob)
            cents = _cents(raw)
            ok = cents is not None and cents in numbers
            src = (_find_source(raws, lambda t: cents in _numbers_in(t)) if ok and cents else None)
        if ok:
            add_source(src)
        else:
            missing.append(raw)

    for q in _quotes(answer):
        checked += 1
        if norm_items is None:
            norm_items = [_norm_text(t) for t in evidence]
        # "…" and a spaced dash both mark a gap between two verbatim pieces
        parts = [_norm_text(p) for p in re.split(r"…|\.\.\.|\s[–—]\s", q)]
        parts = [p for p in parts if p]

        def in_order(hay: str) -> bool:
            pos = 0
            for p in parts:
                i = hay.find(p, pos)
                if i < 0:
                    return False
                pos = i + len(p)
            return True

        ok = bool(parts) and any(in_order(h) for h in norm_items)
        if ok:
            add_source(_find_source(raws, lambda t: in_order(_norm_text(t))))
        else:
            missing.append("„" + q.strip("„“”\"«» ") + "“")

    return Verdict(ok=not missing, missing=missing, sources=sources[:5], checked=checked)


# ─── loop glue ───────────────────────────────────────────────────────

NUDGE_MARK = "[check]"


CHECK_CALL_ID = "check_facts"


def nudge_message(missing: List[str]) -> List[Dict[str, Any]]:
    """Sent back to the model once when a value or quote is not backed.
    Wording approved by Dirk (see chat test report). It comes as the
    result of a check the model "ran" (like the prefetch search), not as
    a message from the person: as a user message the model answered it
    to Mama — "Die Summe 366,44 € taucht in keinem Tool-Ergebnis auf"
    (2026-09-27). Both messages are internal: never evidence, never shown."""
    note = (f"{NUDGE_MARK} Not in any tool result: {', '.join(missing[:6])}. "
            "Look it up and quote it exactly; a number you worked out: compute it with calculate; "
            "otherwise say you did not find it.")
    call = {"id": CHECK_CALL_ID, "type": "function",
            "function": {"name": "check_facts", "arguments": "{}"}}
    return [{"role": "assistant", "internal": True, "content": None, "tool_calls": [call]},
            {"role": "tool", "internal": True, "tool_call_id": CHECK_CALL_ID, "name": "check_facts",
             "content": note}]


# Sentences in which the answer talks about the check instead of to the
# person; a safety net behind the note's new shape.
_CHECK_TALK = re.compile(
    r"(?i)(\[check\]|tool[- ]?(?:ergebnis|result|resultat)|werkzeug[- ]?ergebnis|check_facts)")


def strip_check_talk(text: str) -> str:
    """Drop the sentences that talk about the check."""
    if not text or not _CHECK_TALK.search(text):
        return text
    out_lines = []
    for line in text.splitlines():
        if not _CHECK_TALK.search(line):
            out_lines.append(line)
            continue
        kept = [s for s in re.split(r"(?<=[.!?])\s+", line) if not _CHECK_TALK.search(s)]
        if kept:
            out_lines.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()


def fallback_text(missing: List[str], language: Optional[str], answer: str = "") -> str:
    """Second miss: the answer stays, every unbacked value is marked and an
    unbacked quote is dropped (Dirk 2026-09-27: withholding the whole
    answer threw away the useful rest)."""
    de = (language or "").lower().startswith("de")
    mark = " *(nicht belegt)*" if de else " *(not verified)*"
    text = answer or ""
    quotes = {m.strip("„“") for m in missing if m.startswith("„")}
    if quotes:
        kept = []
        gone = "_(Zitat nicht belegt, weggelassen)_" if de else "_(quote not verified, left out)_"
        for line in text.splitlines():
            body = line.strip().lstrip("> ").strip().strip("„“”\"«» ")
            if line.strip().startswith(">") and body in quotes:
                # a note instead of nothing — "… steht:" must not end in the void
                if not kept or kept[-1] != gone:
                    kept.append(gone)
                continue
            kept.append(line)
        text = "\n".join(kept)
    for value in (m for m in missing if not m.startswith("„")):
        text = re.sub(re.escape(value) + r"(?!\s*\*\((?:nicht belegt|not verified)\))", value + mark, text, count=0)
    if not text.strip():
        text = ("Dazu habe ich nichts Sicheres gefunden." if de else "I found nothing reliable on that.")
    note = ("Werte mit *(nicht belegt)* habe ich in keiner Quelle gefunden." if de
            else "Values marked *(not verified)* were not found in any source.")
    return f"{text.rstrip()}\n\n_{note}_"


def sources_action(verdict: Verdict) -> Optional[Dict[str, Any]]:
    return {"type": "sources", "items": verdict.sources} if verdict.sources else None


def raw_from(name: str, args: Any, result: Any) -> Optional[Tuple[str, Any]]:
    """(skill, raw result) from a dispatched tool, for source labels."""
    meta = getattr(result, "metadata", None) or {}
    raw = meta.get("raw")
    if raw is None:
        return None
    skill = meta.get("skill") or (args.get("name") if isinstance(args, dict) else None) or name
    return (str(skill), raw)


# ─── weekdays ────────────────────────────────────────────────────────

_WD_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_MONTHS = {m: i for i, m in enumerate(
    ["januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september",
     "oktober", "november", "dezember"], 1)}
_WD_DATE = re.compile(
    r"\b(?P<wd>Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)(?P<mid>,?\s+(?:de[nrm]\s+)?)"
    r"(?P<day>\d{1,2})\.\s*(?:(?P<mname>Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|"
    r"November|Dezember)|(?P<mnum>\d{1,2})\.)(?:\s*(?P<year>\d{4}))?", re.I)


def fix_weekdays(text: str, today=None) -> str:
    """A weekday next to a date is checked by the calendar and corrected
    ("Sonntag, 10. Oktober" → "Samstag, …" in 2026). Without a year the
    date nearest to today is meant."""
    from datetime import date, timedelta
    today = today or date.today()

    def repl(m: "re.Match[str]") -> str:
        try:
            day = int(m.group("day"))
            month = _MONTHS[m.group("mname").lower()] if m.group("mname") else int(m.group("mnum"))
            if m.group("year"):
                d = date(int(m.group("year")), month, day)
            else:
                options = [date(today.year + dy, month, day) for dy in (-1, 0, 1)]
                d = min(options, key=lambda x: abs((x - today).days))
        except (ValueError, KeyError):
            return m.group(0)
        right = _WD_DE[d.weekday()]
        if m.group("wd").lower() == right.lower():
            return m.group(0)
        return right + m.group(0)[len(m.group("wd")):]
    return _WD_DATE.sub(repl, text or "")
