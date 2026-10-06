"""Text → text that may leave the house.

Three stages, in this order, each counting what it changed:
  1. the dictionary (pseudonyms.build_dictionary): every known person,
     address, phone, chat, file becomes its token, inflections included
     ("Beates" → person_7);
  2. regexes for what the dictionary cannot know: e-mail addresses,
     IBANs, phone numbers, IP addresses, dates, amounts, long numbers,
     tax ids, secrets — unknown addresses become tokens too, the rest
     placeholders like [date];
  3. the local model names what is left: people, places, organisations
     in prose — only when the model really is local (llm_is_local);
     with a cloud model this stage is skipped and free text is dropped
     altogether (fail closed), the report says so.

Then self_check reads the final bytes once more with stages 1 and 2 and
reports any hit, so a slip in assembling can never be sent. Names in
prose are the weak spot of every such pipeline (research 2026-10-06:
20–90 % missed); that is why the person sees the result before it goes.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from backend.debug_bundle import _EMAIL_RE, _IBAN_RE, _IPV4_RE, _PHONE_RE
from backend.logging_setup import _SECRET_PATTERNS

from . import pseudonyms

log = logging.getLogger("yorik.diagnostics")

_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:?\d{2}|Z)?)?\b|"
                      r"(?<!\d)\d{1,2}\.\s?\d{1,2}\.(?:\s?\d{2,4})?(?!\d)")
_AMOUNT_RE = re.compile(r"(?<![\w.])\d{1,3}(?:[.\s]\d{3})*(?:[,.]\d{1,2})?\s?(?:€|eur|euro|usd|\$|chf)(?!\w)", re.I)
_LONG_NUMBER_RE = re.compile(r"(?<![\w-])\d[\d\s/-]{6,}\d(?![\w-])")
_KEY_RE = re.compile(r"\b(?:sk|or|pk|rk)-[A-Za-z0-9_-]{16,}\b|\b[A-Za-z0-9+/]{40,}={0,2}\b")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_QUOTED_RE = re.compile(r"'[^']{1,120}'|\"[^\"]{1,120}\"|«[^»]{1,120}»|„[^“]{1,120}“")
_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\w+)')
_EXC_CLASS_RE = re.compile(r"^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?=:\s|$)")     # "psycopg.errors.X: …" → psycopg.errors.X

LLM_TIMEOUT_S = 8.0


def llm_is_local(base_url: Optional[str] = None) -> bool:
    """True when the chat model answers from this machine or the LAN —
    loopback, RFC1918, a bare Docker service name, .local — and is not
    OpenRouter. Only then may raw text go to it for the name check."""
    url = base_url if base_url is not None else os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    try:
        from backend.agent.llm import _is_openrouter_base_url
        if _is_openrouter_base_url(url):
            return False
    except Exception:  # noqa: BLE001
        pass
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in ("localhost",) or host.endswith(".local") or "." not in host:
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ─── stage 1 + 2 ─────────────────────────────────────────────────────

def _dictionary_patterns(dictionary) -> List[Tuple[str, str, re.Pattern]]:
    """(kind, canonical value, pattern) per entry; a name part carries
    the full name it belongs to, so "Beates" and "Beate Mayer" share a
    token."""
    pats = []
    for entry in dictionary:
        kind, value = entry[0], entry[1]
        canonical = entry[2] if len(entry) > 2 and entry[2] else value
        esc = re.escape(value)
        if kind == "person" or kind == "org":
            # inflections and genitives: Beates, Mayers, bei Mayer's
            pat = re.compile(r"(?<![\w@])" + esc + r"(?:'?s|n|ns|en)?(?![\w@])", re.I)
        elif kind in ("email", "mailbox", "chat", "group", "file"):
            pat = re.compile(re.escape(value), re.I)
        elif kind == "phone":
            digits = re.sub(r"\D", "", value)
            if len(digits) < 6:
                continue
            pat = re.compile(r"(?<!\d)\+?0{0,2}" + r"[\s/\-()]*".join(re.escape(d) for d in digits[-9:]) + r"(?!\d)")
        elif kind == "iban":
            pat = re.compile(r"[\s]*".join(re.escape(ch) for ch in re.sub(r"\s+", "", value)), re.I)
        else:
            pat = re.compile(re.escape(value))
        pats.append((kind, canonical, pat))
    return pats


def scrub_text(text: str, dictionary, counts: Optional[Dict[str, int]] = None,
               conn=None, mentions: Optional[Dict[str, Tuple[str, str]]] = None) -> Tuple[str, Dict[str, int]]:
    """Stages 1 and 2. Returns (text, counts per kind). `mentions`, when
    given, collects token → (kind, value) for the facts of a report."""
    counts = counts if counts is not None else {}
    out = text or ""
    if not out:
        return out, counts

    def bump(k: str, n: int = 1) -> None:
        if n:
            counts[k] = counts.get(k, 0) + n

    def note(token: Optional[str], kind: str, value: str) -> None:
        if mentions is not None and token:
            mentions.setdefault(token, (kind, value))

    for kind, value, pat in _dictionary_patterns(dictionary):
        if pat.search(out):
            token = pseudonyms.token_for(kind, value, conn=conn) or f"[{kind}]"
            note(token, kind, value)
            out, n = pat.subn(token, out)
            bump(kind, n)
    # what the dictionary did not know
    for pat, kind in ((_EMAIL_RE, "email"), (_IBAN_RE, "iban")):
        def repl(m, kind=kind):
            bump(kind)
            tok = pseudonyms.token_for(kind, m.group(0), conn=conn) or f"[{kind}]"
            note(tok, kind, m.group(0))
            return tok
        out = pat.sub(repl, out)
    out, n = _URL_RE.subn("[url]", out); bump("url", n)
    for pat, _repl in _SECRET_PATTERNS:
        out, n = pat.subn("[secret]", out); bump("secret", n)
    out, n = _KEY_RE.subn("[secret]", out); bump("secret", n)
    out, n = _DATE_RE.subn("[date]", out); bump("date", n)
    out, n = _AMOUNT_RE.subn("[amount]", out); bump("amount", n)

    def phone_repl(m):
        if len(re.sub(r"\D", "", m.group(0))) > 15:        # a parcel or order number, not a phone
            bump("number")
            return "[number]"
        bump("phone")
        return pseudonyms.token_for("phone", m.group(0), conn=conn) or "[phone]"
    out = _PHONE_RE.sub(phone_repl, out)
    out, n = _IPV4_RE.subn("[number]", out); bump("ip", n)
    out, n = _LONG_NUMBER_RE.subn("[number]", out); bump("number", n)
    return re.sub(r"[ \t]+", " ", out).strip(), counts


# ─── stage 3: the local model names people, places, organisations ───

_LLM_PROMPT = (
    "Find every personal name, place name, street, company or organisation name in the text below. "
    "Ignore words that already look like person_3, email_2 or [date]. "
    "Answer only JSON: {\"found\": [{\"text\": \"exact text\", \"kind\": \"person|place|org\"}]}; "
    "if there is none, {\"found\": []}.\n\nText: "
)


def scrub_llm(text: str, counts: Dict[str, int], conn=None) -> Tuple[str, bool]:
    """Stage 3. Returns (text, ran). Never called with a cloud model:
    the caller checks llm_is_local and drops the text instead."""
    if not text.strip():
        return text, True
    import httpx
    from backend import speed
    from backend.agent.llm import _thinking_kwargs_enabled
    body: Dict[str, Any] = {"messages": [{"role": "user", "content": _LLM_PROMPT + text[:1500]}],
                            "temperature": 0.0, "max_tokens": 300}
    model = os.getenv("HOMEOS_MODEL")
    if model:
        body["model"] = model
    if _thinking_kwargs_enabled():
        body["chat_template_kwargs"] = {"enable_thinking": False}
        body["reasoning_effort"] = "none"
    base = os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
    try:
        with httpx.Client(timeout=speed.budget(LLM_TIMEOUT_S, "llm")) as client:
            r = client.post(f"{base}/chat/completions", json=body, headers={"Authorization": "Bearer not-used"})
        r.raise_for_status()
        raw = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        m = re.search(r"\{.*\}", raw, re.S)
        found = json.loads(m.group(0)).get("found") if m else []
    except Exception as exc:  # noqa: BLE001
        log.info("diagnostics: name check by the model failed (%s)", exc)
        return text, False
    out = text
    for item in found if isinstance(found, list) else []:
        t = str(item.get("text") or "").strip()
        kind = str(item.get("kind") or "person")
        if len(t) < 2 or pseudonyms.TOKEN_RE.match(t) or t.startswith("["):
            continue
        repl = pseudonyms.token_for("person", t, conn=conn) if kind == "person" else f"[{'place' if kind == 'place' else 'org'}]"
        out, n = re.subn(r"(?<!\w)" + re.escape(t) + r"(?!\w)", repl or "[name]", out)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return out, True


def scrub_free_text(text: str, dictionary, counts: Dict[str, int],
                    conn=None, mentions: Optional[Dict[str, Tuple[str, str]]] = None) -> Tuple[str, str, bool]:
    """All three stages for prose (the question, a note). Returns
    (text, level, dropped): with a cloud model the text is replaced by
    "" and dropped is True."""
    out, counts = scrub_text(text, dictionary, counts, conn=conn, mentions=mentions)
    if not out:
        return out, "dictionary_regex", False
    if not llm_is_local():
        counts["free_text_dropped"] = counts.get("free_text_dropped", 0) + 1
        return "", "dictionary_regex", True
    out, ran = scrub_llm(out, counts, conn=conn)
    if not ran:
        return "", "dictionary_regex", True
    return out, "dictionary_regex_llm", False


# ─── errors and tool results ─────────────────────────────────────────

def scrub_error(message: str, traceback_text: str = "") -> Dict[str, Any]:
    """An exception as class, places in Yorik's own code and a message
    template — never the text with what the user typed in it."""
    klass = ""
    for line in reversed((traceback_text or "").splitlines()):
        m = _EXC_CLASS_RE.match(line.strip())
        if m:
            klass = m.group(1)
            break
    if not klass:
        m = _EXC_CLASS_RE.match((message or "").strip())
        klass = m.group(1) if m else ("Error" if message else "")
    frames = []
    for path, line, func in _FRAME_RE.findall(traceback_text or ""):
        if "/backend/" in path or path.startswith("backend/"):
            frames.append(f"{path.split('/backend/')[-1].replace('backend/', '')}:{func}:{line}")
    template = message or ""
    template = re.sub(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:\s+", "", template)
    template = _QUOTED_RE.sub("'…'", template)
    template = _URL_RE.sub("[url]", template)
    template = re.sub(r"\d+", "#", template)
    template = _EMAIL_RE.sub("[email]", template)
    template = re.sub(r"\s+", " ", template).strip()[:160]
    return {"class": klass[:80], "frames": ", ".join(frames[-6:])[:400], "template": template}


def route_template(path: str) -> str:
    """/api/contacts/42/channels → /api/contacts/{id}/channels."""
    p = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{27,}", "/{id}", path or "")
    p = re.sub(r"/\d+", "/{id}", p)
    p = re.sub(r"\?.*$", "", p)
    return p[:80]


def result_shape(result: Any) -> Dict[str, Any]:
    """What a tool answered, as a form: size, totals, hits per source,
    whether it failed — never its words."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False) if result is not None else ""
    n = len(text)
    shape: Dict[str, Any] = {"len": "0" if n == 0 else "1-99" if n < 100 else "100-999" if n < 1000 else "1000+",
                             "empty": n == 0}
    data: Any = result
    if isinstance(result, str):
        m = re.search(r"\{.*\}", result, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except ValueError:
            data = None
    if isinstance(data, dict):
        if isinstance(data.get("total"), int):
            shape["total"] = data["total"]
        res = data.get("results")
        if isinstance(res, dict):
            shape["sources"] = {str(k)[:20]: (len(v) if isinstance(v, list) else 0) for k, v in res.items()}
        err = data.get("error")
        if err:
            shape["error_class"] = scrub_error(str(err))["class"] or "Error"
    elif isinstance(text, str) and re.match(r"^\s*(?:error|fehler|exception)\b", text, re.I):
        shape["error_class"] = scrub_error(text)["class"] or "Error"
    return shape


ARG_KINDS = {
    "to": "email", "cc": "email", "bcc": "email", "email": "email", "from": "email", "sender": "email",
    "contact": "person", "name": "person", "person": "person", "who": "person", "recipient": "person",
    "participants": "person", "attendee": "person", "attendees": "person",
    "chat": "chat", "chat_jid": "chat", "jid": "chat", "group": "group",
    "phone": "phone", "number": "phone", "iban": "iban",
    "path": "file", "filename": "file", "file": "file", "attachment": "file",
}
ARG_ENUMS = {"source", "kind", "tone", "template", "status", "category", "language", "period", "mode", "format", "state"}
# the only literal argument values a report may carry
ENUM_VALUES = frozenset("""
email whatsapp paperless immich calendar tasks contacts recordings drafts bank letters pipelines all
friendly formal quick caring firm neutral de en today tomorrow week month year open done draft sent
invoice quote letter dinner meeting conversation pdf text html
""".split())


def scrub_args(args: Dict[str, Any], dictionary, counts: Dict[str, int],
               conn=None, mentions: Optional[Dict[str, Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Argument values only as tokens (known kinds) or as one of
    ENUM_VALUES; everything else is left out, its key stays in arg_keys."""
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        key = str(k).lower()
        if key in ARG_KINDS and isinstance(v, (str, list)):
            vals = v if isinstance(v, list) else [v]
            toks = [pseudonyms.token_for(ARG_KINDS[key], str(x), conn=conn) for x in vals if str(x).strip()]
            toks = [t for t in toks if t]
            if toks:
                out[key] = toks[0]                      # one token per key in v1
                counts[ARG_KINDS[key]] = counts.get(ARG_KINDS[key], 0) + len(toks)
                if mentions is not None:
                    mentions.setdefault(toks[0], (ARG_KINDS[key], str(vals[0])))
        elif key in ARG_ENUMS and isinstance(v, str) and v.strip().lower() in ENUM_VALUES:
            out[key] = v.strip().lower()
    return out


# ─── the last look ───────────────────────────────────────────────────

def self_check(payload_text: str, dictionary: List[Tuple[str, str]]) -> List[str]:
    """Kinds of personal data still found in the final bytes. Empty
    means clear to send. Reports kinds, never the values."""
    hits: List[str] = []
    for kind, value, pat in _dictionary_patterns(dictionary):
        if len(value) >= 4 and pat.search(payload_text):
            hits.append(kind)
    for pat, kind in ((_EMAIL_RE, "email"), (_IBAN_RE, "iban"), (_PHONE_RE, "phone"), (_IPV4_RE, "ip")):
        if pat.search(payload_text):
            hits.append(kind)
    for pat, _ in _SECRET_PATTERNS:
        if pat.search(payload_text):
            hits.append("secret")
    if _KEY_RE.search(payload_text):
        hits.append("secret")
    return sorted(set(hits))
