"""Assembling an error report from one turn of a conversation.

What goes in (and only through registry.serialise): the question with
every known person, address and chat as a token, the kind of request,
whether the answer said "found nothing", the tools that ran as names,
argument keys and result shapes, facts about the things the question
mentions (does Yorik have rows about person_7? indexed? where does the
search put them today?), the skill calls and errors of the turn as
class and template, the environment in buckets, the thumb and reason.
Never the text of a mail, a document or a message.

The person sees the result (routes: draft → review → send/decline);
queue() runs the last look (scrub.self_check) before anything is
queued for the outbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.database import get_conn

from . import SCHEMA_VERSION, enabled, pseudonyms, registry, scrub, secret

log = logging.getLogger("yorik.diagnostics")

NOTHING_FOUND = re.compile(
    r"nichts (?:\w+ )?(?:gefunden|finden)|keine[nrs]? \w+ (?:\w+ )?gefunden|nicht finden|finde (?:ich )?(?:leider )?(?:nichts|keine)|"
    r"konnte (?:ich )?(?:leider )?(?:nichts|keine)|liegt (?:mir )?(?:nichts|keine)|"
    r"found nothing|couldn'?t find|could not find|no \w+ (?:was |were )?found|nothing (?:that )?match", re.I)
_DE = set("der die das und ist nicht ich du wir mit von für auf dem den eine einen hat hast war wo was wie wann".split())
_EN = set("the and is not you we with for on from what where when have has was did my your".split())


# ─── the turn ────────────────────────────────────────────────────────

def _is_interim(msgs: List[Any], i: int) -> bool:
    m = msgs[i]
    if m.get("role") != "assistant" or not m.get("tool_calls"):
        return False
    for later in msgs[i + 1:]:
        if not isinstance(later, dict):
            continue
        if later.get("role") == "user":
            return False
        if later.get("role") == "assistant" and (later.get("content") or "").strip():
            return True
    return False


def display_messages(msgs: List[Any]) -> List[Tuple[int, Dict[str, Any]]]:
    """The messages the chat shows, with their index in the stored
    list — the same filter as GET /api/conversations/{id}, so the
    chat's message_idx lands on the right turn."""
    out = []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or m.get("internal"):
            continue
        if m.get("role") == "assistant" and not (m.get("content") or "").strip():
            continue
        if _is_interim(msgs, i):
            continue
        out.append((i, m))
    return out


def turn_of(msgs: List[Any], message_idx: int) -> Dict[str, Any]:
    """The answer at display index `message_idx`, the question before
    it, and the tool calls between."""
    shown = display_messages(msgs)
    if message_idx < 0 or message_idx >= len(shown):
        raise KeyError("no such message")
    stored_idx, answer = shown[message_idx]
    if answer.get("role") != "assistant":
        raise KeyError("not an answer")
    q_idx, question = None, None
    for j in range(stored_idx - 1, -1, -1):
        m = msgs[j]
        if isinstance(m, dict) and m.get("role") == "user":
            q_idx, question = j, m
            break
    calls: List[Dict[str, Any]] = []
    meta = answer.get("metadata") if isinstance(answer.get("metadata"), dict) else {}
    thin = meta.get("tool_trace") or answer.get("tool_trace")
    if isinstance(thin, list) and thin:
        for e in thin:
            if isinstance(e, dict):
                calls.append({"name": str(e.get("name") or ""), "args": e.get("args") if isinstance(e.get("args"), dict) else {},
                              "result": e.get("result")})
    else:
        results: Dict[str, Any] = {}
        for m in msgs[(q_idx or 0):stored_idx + 1]:
            if isinstance(m, dict) and m.get("role") == "tool":
                results[str(m.get("tool_call_id"))] = m.get("content")
        for m in msgs[(q_idx or 0):stored_idx + 1]:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                name = fn.get("name") or ""
                if name == "invoke_skill" and isinstance(args, dict):
                    name = str(args.get("name") or name)
                    args = args.get("args") if isinstance(args.get("args"), dict) else {}
                calls.append({"name": name, "args": args, "result": results.get(str(tc.get("id")))})
    text = answer.get("content")
    if isinstance(text, list):
        text = " ".join(str(x.get("text") or "") for x in text if isinstance(x, dict))
    qtext = question.get("content") if question else ""
    if isinstance(qtext, list):
        qtext = " ".join(str(x.get("text") or "") for x in qtext if isinstance(x, dict))
    return {"question": str(qtext or ""), "answer": str(text or ""), "calls": calls, "stored_idx": stored_idx}


# ─── buckets ─────────────────────────────────────────────────────────

def bucket(n: int, edges: List[Tuple[int, str]]) -> str:
    for upto, label in edges:
        if n <= upto:
            return label
    return edges[-1][1]


B_COUNT = [(0, "0"), (9, "1-9"), (99, "10-99"), (499, "100-499"), (10**9, "500+")]
B_TURNS = [(0, "0"), (9, "1-9"), (49, "10-49"), (199, "50-199"), (10**9, "200+")]
B_WORDS_Q = [(3, "1-3"), (9, "4-9"), (24, "10-24"), (10**9, "25+")]
B_WORDS_A = [(0, "0"), (9, "1-9"), (49, "10-49"), (199, "50-199"), (10**9, "200+")]
B_LATENCY = [(999, "<1s"), (2999, "1-3s"), (9999, "3-10s"), (10**9, "10s+")]
B_USERS = [(1, "1"), (2, "2"), (4, "3-4"), (10**9, "5+")]


def environment() -> Dict[str, Any]:
    """Version, platform and model, each as a bucket or enum."""
    raw = os.getenv("YORIK_VERSION") or ""
    m = re.match(r"v?(\d+)\.(\d+)", raw)
    version = f"{m.group(1)}.{m.group(2)}" if m else "dev"
    if os.path.exists("/.dockerenv") or raw:
        plat = "docker"
    elif sys.platform.startswith("linux"):
        plat = "linux-arm64" if "arm" in platform.machine() or "aarch" in platform.machine() else "linux-x64"
    elif sys.platform == "darwin":
        plat = "mac"
    elif sys.platform.startswith("win"):
        plat = "win"
    else:
        plat = "other"
    base = os.getenv("HOMEOS_LLM_BASE_URL", "")
    model = (os.getenv("HOMEOS_MODEL") or "").lower()
    if not model:
        kind = "none"
    elif scrub.llm_is_local(base):
        kind = "local"
    elif "openrouter" in base:
        kind = "cloud-openrouter"
    else:
        kind = "cloud-other"
    family = next((f for f in registry.LLM_FAMILIES if f in model), "other")
    age = "<1w"
    try:
        with get_conn() as conn:
            r = conn.execute("SELECT min(created_at) AS t FROM user_profiles").fetchone()
        if r and r["t"]:
            first = datetime.fromisoformat(str(r["t"]).replace("Z", "+00:00"))
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - first).days
            age = "<1w" if days < 7 else "1-4w" if days < 28 else "1-6m" if days < 180 else "6m+"
    except Exception:  # noqa: BLE001
        pass
    return {"version": version, "platform": plat, "llm_kind": kind, "llm_family": family, "install_age": age}


def intent_of(question: str) -> str:
    from backend.agent import prefetch
    if prefetch._COMMAND.search(question or ""):
        return "command"
    if prefetch._AGENDA.search(question or ""):
        return "agenda"
    if prefetch.should_search(question or ""):
        return "search"
    if prefetch._QUESTION.search(question or ""):
        return "question"
    return "other"


def language_of(text: str) -> str:
    words = set(re.findall(r"[a-zäöüß]+", (text or "").lower()))
    de, en = len(words & _DE), len(words & _EN)
    return "de" if de > en else "en" if en > de else "other"


# ─── facts ───────────────────────────────────────────────────────────

def _month(s: Any) -> Optional[str]:
    t = str(s or "")
    m = re.match(r"(\d{4})-(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    if t.isdigit():
        try:
            return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m")
        except (ValueError, OSError):
            return None
    return None


def facts_for(mentions: Dict[str, Tuple[str, str]], user_id: str, question: str, search_hits: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """For every token in the report: does Yorik have rows about it, are
    they indexed, when was the newest, where does the search put it."""
    out: List[Dict[str, Any]] = []
    with get_conn() as conn:
        for token, (kind, value) in mentions.items():
            if kind not in registry.FACT_KINDS:
                continue
            rows, last, indexed, rank = 0, None, False, 0
            like = f"%{value.lower()}%"
            try:
                if kind in ("email", "mailbox"):
                    r = conn.execute("SELECT count(*) AS n, max(date_received) AS t FROM email_messages WHERE owner_user_id = ? "
                                     "AND lower(from_email) = ?", (user_id, value.lower())).fetchone()
                    rows, last = int(r["n"] or 0), _month(r["t"])
                    if rows:
                        indexed = bool(conn.execute("SELECT 1 FROM search_chunks sc JOIN email_messages m ON m.id = sc.row_id "
                                                    "WHERE sc.source = 'email' AND m.owner_user_id = ? AND lower(m.from_email) = ? LIMIT 1",
                                                    (user_id, value.lower())).fetchone())
                    rank = _rank(search_hits.get("email", []), lambda h: value.lower() in str(h.get("subtitle") or "").lower()
                                 or value.lower() in str(h.get("title") or "").lower())
                elif kind in ("person", "org"):
                    r = conn.execute("SELECT count(*) AS n, max(date_received) AS t FROM email_messages WHERE owner_user_id = ? "
                                     "AND (lower(from_name) LIKE ? OR lower(subject) LIKE ?)", (user_id, like, like)).fetchone()
                    rows, last = int(r["n"] or 0), _month(r["t"])
                    c = conn.execute("SELECT count(*) AS n FROM contacts WHERE status IN ('active','pending') AND lower(display_name) LIKE ?",
                                     (like,)).fetchone()["n"]
                    w = conn.execute("SELECT count(*) AS n, max(timestamp) AS t FROM wa_messages m JOIN wa_chats c ON c.jid = m.chat_jid "
                                     "AND c.owner_user_id = m.owner_user_id WHERE m.owner_user_id = ? AND (lower(c.name) LIKE ? OR lower(m.push_name) LIKE ?)",
                                     (user_id, like, like)).fetchone()
                    rows += int(c or 0) + int(w["n"] or 0)
                    last = last or _month(w["t"])
                    indexed = rows > 0 and bool(conn.execute(
                        "SELECT 1 FROM search_chunks sc WHERE sc.source IN ('email','whatsapp','contacts') AND lower(sc.text) LIKE ? LIMIT 1",
                        (like,)).fetchone())
                    rank = min((x for x in (_rank(search_hits.get(src, []), lambda h: value.lower() in (str(h.get("title") or "") + " " + str(h.get("subtitle") or "") + " " + str(h.get("who") or "")).lower())
                                           for src in ("email", "whatsapp", "contacts")) if x), default=0)
                elif kind in ("chat", "group"):
                    r = conn.execute("SELECT count(*) AS n, max(timestamp) AS t FROM wa_messages WHERE owner_user_id = ? AND lower(chat_jid) = ?",
                                     (user_id, value.lower())).fetchone()
                    rows, last = int(r["n"] or 0), _month(r["t"])
                    indexed = rows > 0 and bool(conn.execute("SELECT 1 FROM search_chunks sc JOIN wa_messages m ON m.id = sc.row_id "
                                                             "WHERE sc.source = 'whatsapp' AND lower(m.chat_jid) = ? LIMIT 1", (value.lower(),)).fetchone())
                    rank = _rank(search_hits.get("whatsapp", []), lambda h: str(h.get("chat_jid") or "").lower() == value.lower())
                elif kind == "phone":
                    rows = int(conn.execute("SELECT count(*) AS n FROM contact_channels WHERE kind IN ('phone','whatsapp') AND value LIKE ?",
                                            (f"%{re.sub(r'[^0-9]', '', value)[-8:]}%",)).fetchone()["n"] or 0)
                elif kind == "file":
                    rows = int(conn.execute("SELECT count(*) AS n FROM docs.paperless_documents WHERE lower(title) LIKE ?", (like,)).fetchone()["n"] or 0)
            except Exception as exc:  # noqa: BLE001 — a fact that cannot be read is simply absent
                log.info("diagnostics: fact for %s skipped: %s", token, exc)
            fact = {"token": token, "kind": kind, "exists": rows > 0, "indexed": bool(indexed),
                    "rows": bucket(rows, B_COUNT), "search_rank": min(rank, 50), "recomputed": True}
            if last:
                fact["last_seen"] = last
            out.append(fact)
    return out


def _rank(hits: List[Dict[str, Any]], match) -> int:
    for i, h in enumerate(hits, 1):
        try:
            if match(h):
                return i
        except Exception:  # noqa: BLE001
            continue
    return 0


def _search_today(question: str, user_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """The search as the chat would run it now, for the ranks in facts."""
    try:
        from backend.agent import prefetch
        user = {"id": user_id, "role": "member"}

        async def run():
            query, _ = await prefetch.prepare(question, user)
            return await prefetch.search(question, query, [], user)
        raw = asyncio.run(run())
        return raw.get("results") or {}
    except Exception as exc:  # noqa: BLE001
        log.info("diagnostics: search for facts skipped: %s", exc)
        return {}


# ─── assembling ──────────────────────────────────────────────────────

def assemble(conversation_id: str, message_idx: int, user_id: str, trigger: str,
             reason: Optional[str] = None, note: Optional[str] = None) -> Dict[str, Any]:
    """A draft report for one answer, stored with status 'draft'."""
    from backend.agent.conversation_io import load_messages
    msgs = load_messages(conversation_id, user_id)
    if not msgs:
        raise KeyError("no such conversation")
    turn = turn_of(msgs, message_idx)
    dictionary = pseudonyms.build_dictionary(user_id)
    counts: Dict[str, int] = {}
    mentions: Dict[str, Tuple[str, str]] = {}
    with get_conn() as conn:
        q_text, level, q_dropped = scrub.scrub_free_text(turn["question"], dictionary, counts, conn=conn, mentions=mentions)
        n_text, _, n_dropped = scrub.scrub_free_text(note or "", dictionary, counts, conn=conn, mentions=mentions) if note else ("", level, False)
        trace = []
        for c in turn["calls"][:12]:
            args = c.get("args") or {}
            trace.append({"name": str(c.get("name") or "")[:60],
                          "arg_keys": ",".join(sorted(str(k) for k in args))[:200],
                          "args": scrub.scrub_args(args, dictionary, counts, conn=conn, mentions=mentions),
                          "result": scrub.result_shape(c.get("result"))})
        conn.commit()
    rating = "none"
    with get_conn() as conn:
        fb = conn.execute("SELECT rating FROM turn_feedback WHERE conversation_id = ? AND message_idx = ? AND user_id = ? "
                          "ORDER BY created_at DESC LIMIT 1", (conversation_id, message_idx, user_id)).fetchone()
        if fb:
            rating = "up" if int(fb["rating"] or 0) > 0 else "down" if int(fb["rating"] or 0) < 0 else "none"
        skills = []
        for r in conn.execute("SELECT skill_id, success, error, latency_ms FROM skill_invocations WHERE conversation_id = ? "
                              "ORDER BY created_at DESC LIMIT 20", (conversation_id,)).fetchall():
            e = scrub.scrub_error(r["error"] or "") if r["error"] else {"class": "", "template": ""}
            skills.append({"skill": str(r["skill_id"])[:60], "success": bool(r["success"]),
                           "error_class": e["class"], "error_template": e["template"],
                           "latency": bucket(int(r["latency_ms"] or 0), B_LATENCY)})
        errors = []
        try:
            for r in conn.execute("SELECT message, traceback, request_path FROM error_log WHERE level IN ('ERROR','CRITICAL') "
                                  "AND ts > to_char(now() - interval '1 day', 'YYYY-MM-DD HH24:MI:SS') ORDER BY ts DESC LIMIT 5").fetchall():
                e = scrub.scrub_error(r["message"] or "", r["traceback"] or "")
                errors.append({"class": e["class"], "frames": e["frames"], "template": e["template"],
                               "route": scrub.route_template(r["request_path"] or "")})
        except Exception:  # noqa: BLE001
            errors = []
    hits = _search_today(turn["question"], user_id) if mentions else {}
    facts = facts_for(mentions, user_id, turn["question"], hits)
    report_id = str(uuid.uuid4())
    payload = {
        "schema": SCHEMA_VERSION, "kind": "error", "report_id": report_id, "trigger": trigger,
        "env": environment(),
        "feedback": {"rating": rating, "reason": reason if reason in registry.REASONS else "none", "note": n_text},
        "question": {"text": q_text, "intent": intent_of(turn["question"]), "words": bucket(len(turn["question"].split()), B_WORDS_Q),
                     "language": language_of(turn["question"]),
                     "with_previous": any("with_previous_question" in (c.get("args") or {}) for c in turn["calls"])},
        "answer": {"said_nothing_found": bool(NOTHING_FOUND.search(turn["answer"])),
                   "words": bucket(len(turn["answer"].split()), B_WORDS_A), "tool_calls": len(turn["calls"])},
        "trace": trace, "facts": facts, "skills": skills, "errors": errors,
        "scrub": {"level": level, "replaced": {k: v for k, v in counts.items() if k != "free_text_dropped"},
                  "dropped_fields": 0, "free_text_dropped": bool(q_dropped or n_dropped)},
    }
    clean, dropped = registry.serialise(payload, max_tier=3)
    clean.setdefault("scrub", {})["dropped_fields"] = min(dropped, 50)
    summary = {"replaced": clean["scrub"].get("replaced", {}), "dropped_fields": dropped,
               "free_text_dropped": clean["scrub"].get("free_text_dropped", False), "level": level,
               "mentions": sorted(mentions)}
    with get_conn() as conn:
        conn.execute("INSERT INTO diag_reports (id, user_id, conversation_id, message_idx, kind, trigger, status, payload, scrub_summary) "
                     "VALUES (?, ?, ?, ?, 'error', ?, 'draft', ?, ?)",
                     (report_id, user_id, conversation_id, message_idx, trigger, json.dumps(clean, ensure_ascii=False),
                      json.dumps(summary, ensure_ascii=False)))
        conn.commit()
    log.info("diagnostics: report %s drafted (%s, %d fields dropped)", report_id, trigger, dropped)
    return {"id": report_id, "status": "draft", "payload": clean, "scrub_summary": summary}


def _load(report_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM diag_reports WHERE id = ?", (report_id,)).fetchone()
    return dict(r) if r else None


def delete_token(report_id: str) -> str:
    return hmac.new(secret(), f"delete\0{report_id}".encode(), hashlib.sha256).hexdigest()


def queue(report_id: str, user_id: str, edited_question: Optional[str] = None,
          edited_note: Optional[str] = None) -> Dict[str, Any]:
    """After the person's review: the last look, then the queue. The
    person may have shortened the question or the note; the shortened
    text goes through the scrubber again."""
    row = _load(report_id)
    if not row:
        raise KeyError("no such report")
    if str(row["user_id"]) != str(user_id):
        raise PermissionError("not your report")
    if row["status"] != "draft":
        raise ValueError(f"report is {row['status']}")
    if not enabled(3):
        raise PermissionError("diagnostics_disabled")
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
    dictionary = pseudonyms.build_dictionary(user_id)
    counts: Dict[str, int] = {}
    if edited_question is not None or edited_note is not None:
        with get_conn() as conn:
            if edited_question is not None:
                t, _, _ = scrub.scrub_free_text(edited_question[:400], dictionary, counts, conn=conn)
                payload.setdefault("question", {})["text"] = t
            if edited_note is not None:
                t, _, _ = scrub.scrub_free_text(edited_note[:300], dictionary, counts, conn=conn)
                payload.setdefault("feedback", {})["note"] = t
            conn.commit()
        payload, _ = registry.serialise(payload, max_tier=3)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    hits = scrub.self_check(text, dictionary)
    if hits:
        raise ValueError("personal data still in the report: " + ", ".join(hits))
    tok = delete_token(report_id)
    payload_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with get_conn() as conn:
        conn.execute("UPDATE diag_reports SET status = 'queued', payload = ?, next_attempt_at = now(), delete_token = ?, "
                     "last_error = NULL WHERE id = ?", (text, tok, report_id))
        conn.commit()
    log.info("diagnostics: report %s queued by its person (sha256 %s)", report_id, payload_hash[:12])
    try:
        from . import outbox
        outbox.wake()
    except Exception:  # noqa: BLE001
        pass
    return {"id": report_id, "status": "queued", "sha256": payload_hash}


def decline(report_id: str, user_id: str) -> Dict[str, Any]:
    row = _load(report_id)
    if not row:
        raise KeyError("no such report")
    if str(row["user_id"]) != str(user_id):
        raise PermissionError("not your report")
    with get_conn() as conn:
        conn.execute("UPDATE diag_reports SET status = 'declined', payload = '{}' WHERE id = ? AND status = 'draft'", (report_id,))
        conn.commit()
    return {"id": report_id, "status": "declined"}
