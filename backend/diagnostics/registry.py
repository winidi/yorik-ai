"""The allow-list of what a diagnostics payload may carry.

Every field that can leave the house is listed here with its purpose,
its tier and its type; `serialise` walks a payload and keeps only
listed paths whose values pass the type check. Anything else is
dropped and counted, never scrubbed — a field nobody wrote down cannot
be sent by accident (Mozilla's metrics.yaml idea, kept as Python so
there is no loader). The privacy page shows this list to the person as
"every field Yorik can send".

Types: enum (one of the listed values), bucket (one of the listed
bucket strings), smallint (0..50, clipped), bool, month (YYYY-MM),
token (a pseudonym like person_7 or a placeholder like [place]), text
(scrubbed text, length-capped), id (the report's own random id),
version (x.y), day (YYYY-MM-DD, for daily counts).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

TOKEN_RE = re.compile(r"^(?:person|email|phone|iban|chat|group|mailbox|file|secret|org)_\d+$|^\[(?:place|org|name|date|amount|number|id|secret|url)\]$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION_RE = re.compile(r"^\d+\.\d+$|^dev$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

USERS_BUCKETS = ("1", "2", "3-4", "5+")
COUNT_BUCKETS = ("0", "1-9", "10-99", "100-499", "500+")
TURNS_BUCKETS = ("0", "1-9", "10-49", "50-199", "200+")
LEN_BUCKETS = ("0", "1-99", "100-999", "1000+")
LATENCY_BUCKETS = ("<1s", "1-3s", "3-10s", "10s+")
AGE_BUCKETS = ("<1w", "1-4w", "1-6m", "6m+")
PLATFORMS = ("linux-x64", "linux-arm64", "docker", "mac", "win", "other")
LLM_KINDS = ("local", "cloud-openrouter", "cloud-other", "none")
LLM_FAMILIES = ("qwen", "llama", "mistral", "gemma", "phi", "deepseek", "gpt", "claude", "gemini", "other")
TRIGGERS = ("thumbs_down", "report_problem", "skill_failure", "search_empty", "exception", "daily")
REASONS = ("wrong_answer", "found_nothing", "wrong_person", "too_slow", "error", "other", "none")
INTENTS = ("question", "command", "agenda", "search", "write", "other")
LANGS = ("de", "en", "other")
SCRUB_LEVELS = ("dictionary_regex_llm", "dictionary_regex")
SOURCES = ("email", "whatsapp", "paperless", "immich", "calendar", "tasks", "contacts", "recordings",
           "drafts", "bank", "letters", "pipelines", "other")
FACT_KINDS = ("person", "email", "phone", "chat", "group", "mailbox", "file", "org", "iban")


@dataclass(frozen=True)
class Field:
    path: str                   # dotted path; [] = every list item; * = any key at that level
    tier: int                   # 1 counts, 2 usage, 3 errors
    type: str                   # see module docstring
    purpose: str
    values: Tuple[str, ...] = field(default_factory=tuple)
    max_len: int = 0
    expires: str = "2027-12"    # when to re-justify the field


def _f(path, tier, type_, purpose, values=(), max_len=0) -> Field:
    return Field(path, tier, type_, purpose, tuple(values), max_len)


FIELDS: List[Field] = [
    # ── every payload ─────────────────────────────────────────────
    _f("schema", 1, "smallint", "payload layout version"),
    _f("kind", 1, "enum", "what this payload is", ("error", "usage_daily")),
    _f("report_id", 1, "id", "random id of this payload, for deletion requests"),
    _f("env.version", 1, "version", "Yorik version (major.minor) the report comes from"),
    _f("env.platform", 1, "enum", "operating system / install type", PLATFORMS),
    _f("env.llm_kind", 1, "enum", "whether the model runs locally or in a cloud", LLM_KINDS),
    _f("env.llm_family", 1, "enum", "model family, for failures that depend on it", LLM_FAMILIES),
    _f("env.install_age", 1, "bucket", "how long this installation exists", AGE_BUCKETS),
    _f("scrub.level", 3, "enum", "which scrubbing stages ran", SCRUB_LEVELS),
    _f("scrub.replaced.*", 3, "smallint", "how many values of each kind became tokens"),
    _f("scrub.dropped_fields", 3, "smallint", "fields the allow-list threw away"),
    _f("scrub.free_text_dropped", 3, "bool", "free text left out because no local model could check names"),
    # ── usage_daily (tiers 1 and 2) ───────────────────────────────
    _f("day", 1, "day", "the day the counts are for"),
    _f("counts.users", 1, "bucket", "people in the household", USERS_BUCKETS),
    _f("counts.chat_turns", 1, "bucket", "chat questions that day", TURNS_BUCKETS),
    _f("counts.feedback_up", 1, "smallint", "thumbs up that day"),
    _f("counts.feedback_down", 1, "smallint", "thumbs down that day"),
    _f("counts.skill_failures", 1, "smallint", "skill calls that failed that day"),
    _f("features.*", 2, "bucket", "how often each kind of feature was used (mail, whatsapp, documents, calendar, tasks, bank, search, letters, recordings, voice, other)", TURNS_BUCKETS),
    _f("setup.mailboxes", 2, "bucket", "connected mail accounts", COUNT_BUCKETS),
    _f("setup.whatsapp", 2, "bool", "WhatsApp connected"),
    _f("setup.documents", 2, "bucket", "documents in Paperless", COUNT_BUCKETS),
    _f("setup.contacts", 2, "bucket", "contacts", COUNT_BUCKETS),
    _f("setup.bank", 2, "bool", "bank account connected"),
    # ── error report (tier 3) ─────────────────────────────────────
    _f("trigger", 3, "enum", "what started the report", TRIGGERS),
    _f("feedback.rating", 3, "enum", "thumb", ("up", "down", "none")),
    _f("feedback.reason", 3, "enum", "the reason chip", REASONS),
    _f("feedback.note", 3, "text", "the person's note, scrubbed and reviewed", max_len=300),
    _f("question.text", 3, "text", "the question, pseudonymised and reviewed", max_len=400),
    _f("question.intent", 3, "enum", "the kind of request", INTENTS),
    _f("question.words", 3, "bucket", "length of the question", ("1-3", "4-9", "10-24", "25+")),
    _f("question.language", 3, "enum", "language of the question", LANGS),
    _f("question.with_previous", 3, "bool", "a follow-up that carried the previous question's words"),
    _f("answer.said_nothing_found", 3, "bool", "the answer says it found nothing"),
    _f("answer.words", 3, "bucket", "length of the answer", ("0", "1-9", "10-49", "50-199", "200+")),
    _f("answer.tool_calls", 3, "smallint", "tools the model used in this turn"),
    _f("trace[].name", 3, "text", "tool or skill name", max_len=60),
    _f("trace[].arg_keys", 3, "text", "argument names, comma-separated", max_len=200),
    _f("trace[].args.*", 3, "token_or_enum", "argument values of known kinds as tokens, or one of a fixed list of plain words"),
    _f("trace[].result.len", 3, "bucket", "size of the tool's answer", LEN_BUCKETS),
    _f("trace[].result.total", 3, "smallint", "hits the tool reported"),
    _f("trace[].result.sources.*", 3, "smallint", "hits per source"),
    _f("trace[].result.error_class", 3, "text", "error class if the tool failed", max_len=80),
    _f("trace[].result.empty", 3, "bool", "the tool returned nothing"),
    _f("trace[].duration", 3, "bucket", "how long the tool took", LATENCY_BUCKETS),
    _f("facts[].token", 3, "token", "the pseudonym the fact is about"),
    _f("facts[].kind", 3, "enum", "what kind of thing", FACT_KINDS),
    _f("facts[].exists", 3, "bool", "Yorik has rows about it"),
    _f("facts[].indexed", 3, "bool", "its rows are in the search index"),
    _f("facts[].rows", 3, "bucket", "how many rows mention it", COUNT_BUCKETS),
    _f("facts[].last_seen", 3, "month", "month of the newest row"),
    _f("facts[].search_rank", 3, "smallint", "where the search puts it today (0 = not found)"),
    _f("facts[].recomputed", 3, "bool", "rank measured at report time, not at the question"),
    _f("skills[].skill", 3, "text", "skill id", max_len=60),
    _f("skills[].success", 3, "bool", "whether it succeeded"),
    _f("skills[].error_class", 3, "text", "error class", max_len=80),
    _f("skills[].error_template", 3, "text", "error message with every quoted or numeric part removed", max_len=160),
    _f("skills[].latency", 3, "bucket", "how long it took", LATENCY_BUCKETS),
    _f("errors[].class", 3, "text", "exception class", max_len=80),
    _f("errors[].frames", 3, "text", "places in Yorik's own code (module:function:line), comma-separated", max_len=400),
    _f("errors[].template", 3, "text", "message template", max_len=160),
    _f("errors[].route", 3, "text", "API route as template, ids replaced", max_len=80),
]

_BY_PATH: Dict[str, Field] = {f.path: f for f in FIELDS}


def by_tier(max_tier: int) -> List[Field]:
    return [f for f in FIELDS if f.tier <= max_tier]


def describe() -> List[Dict[str, Any]]:
    """For the privacy page: every field, plain."""
    return [{"path": f.path, "tier": f.tier, "type": f.type, "purpose": f.purpose,
             "values": list(f.values) if f.values else None, "max_len": f.max_len or None} for f in FIELDS]


def _check(f: Field, v: Any) -> Optional[Any]:
    """The value if it fits the field's type, else None."""
    t = f.type
    if t == "enum":
        return v if isinstance(v, str) and v in f.values else None
    if t == "bucket":
        return v if isinstance(v, str) and v in f.values else None
    if t == "smallint":
        if isinstance(v, bool) or not isinstance(v, int):
            return None
        return max(0, min(50, v))
    if t == "bool":
        return v if isinstance(v, bool) else None
    if t == "month":
        return v if isinstance(v, str) and MONTH_RE.match(v) else None
    if t == "day":
        return v if isinstance(v, str) and DAY_RE.match(v) else None
    if t == "token":
        return v if isinstance(v, str) and TOKEN_RE.match(v) else None
    if t == "token_or_enum":
        if not isinstance(v, str):
            return None
        if TOKEN_RE.match(v):
            return v
        from .scrub import ENUM_VALUES
        return v if v in ENUM_VALUES else None
    if t == "text":
        if not isinstance(v, str):
            return None
        return v[: f.max_len or 200]
    if t == "id":
        return v if isinstance(v, str) and UUID_RE.match(v) else None
    if t == "version":
        return v if isinstance(v, str) and VERSION_RE.match(v) else None
    return None


def _lookup(path: str) -> Optional[Field]:
    if path in _BY_PATH:
        return _BY_PATH[path]
    head, _, last = path.rpartition(".")
    return _BY_PATH.get(f"{head}.*") if head else None


def serialise(payload: Dict[str, Any], max_tier: int) -> Tuple[Dict[str, Any], int]:
    """Only registered fields up to `max_tier`, values type-checked;
    returns (clean payload, number of dropped leaves)."""
    dropped = 0

    def walk(obj: Any, path: str) -> Any:
        nonlocal dropped
        if isinstance(obj, dict):
            out: Dict[str, Any] = {}
            for k, v in obj.items():
                sub = f"{path}.{k}" if path else str(k)
                if isinstance(v, (dict, list)):
                    inner = walk(v, sub)
                    if inner not in ({}, [], None):
                        out[k] = inner
                    continue
                f = _lookup(sub)
                if f is None or f.tier > max_tier:
                    dropped += 1
                    continue
                checked = _check(f, v)
                if checked is None:
                    dropped += 1
                    continue
                out[k] = checked
            return out
        if isinstance(obj, list):
            items = [walk(x, f"{path}[]") for x in obj]
            return [x for x in items if x not in ({}, None)]
        dropped += 1
        return None

    return walk(payload, ""), dropped
