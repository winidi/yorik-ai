"""Universal search — one query across email, WhatsApp, Paperless,
Immich, calendar, tasks, contacts, recordings and drafts.

Each source returns up to 5 hits, all in parallel via asyncio.gather.
Total budget is ~500ms — slow sources (Immich CLIP can be 1-2s)
don't block the fast ones.

Sources with rows in Yorik's own database are hybrid: keyword hits
first, then hits by meaning from the semantic index
(backend/search_index.py), both behind the same visibility rule the
app applies (owner, spaces, row shares — no admin exception).

Source results share a common shape so the frontend can render them
in a uniform list:

    {
        "source": "email" | "whatsapp" | "paperless" | "immich" | "calendar"
                  | "tasks" | "contacts" | "recordings" | "drafts",
        "id": <opaque id used to deep-link>,
        "title": <main label, e.g. email subject>,
        "subtitle": <secondary label, e.g. sender name>,
        "snippet": <body preview, ≤200 chars>,
        "timestamp": <ISO or epoch, when this thing happened>,
        "navigate_to": <relative URL the UI should open on click>,
        "thumbnail_url": <optional, for photos>,
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .auth_sessions import current_user
from .database import get_conn

log = logging.getLogger("yorik.search")


def _build_tsquery(query: str) -> Optional[str]:
    """Turn a user query like "Müller invoice 2025" into a Postgres
    tsquery string: tokens AND-joined with prefix matching ("müller &
    invoice:* & 2025:*"). Strips characters that have special meaning
    in tsquery syntax so a stray `:` or `&` from the user doesn't blow
    up the planner. Returns None when no usable tokens remain."""
    raw = [t for t in (query or "").split() if t]
    cleaned = [re.sub(r"[^\w\-]", "", t, flags=re.UNICODE) for t in raw]
    cleaned = [t for t in cleaned if t]
    if not cleaned:
        return None
    return " & ".join(f"{t}:*" for t in cleaned)

def _build_tsquery_any(query: str) -> Optional[str]:
    """Like _build_tsquery, but any word may match (OR); ranking decides.
    A whole question never has every word in one mail."""
    cleaned = [re.sub(r"[^\w\-]", "", t, flags=re.UNICODE) for t in (query or "").split()]
    cleaned = [t for t in cleaned if len(t) > 2]
    return " | ".join(f"{t}:*" for t in cleaned[:8]) or None


def _words_regex(query: str) -> Optional[str]:
    words = [re.escape(w.lower()) for w in re.findall(r"\w+", query or "") if len(w) > 2][:8]
    return "|".join(words) or None


def _query_words(query: str, limit: int = 8) -> list[str]:
    words: list[str] = []
    for t in (query or "").split():
        w = re.sub(r"[^\w\-]", "", t, flags=re.UNICODE)
        if len(w) > 2 and w.lower() not in (x.lower() for x in words):
            words.append(w)
    return words[:limit]


# A word in more than this share of the person's rows says nothing about
# which row they mean ("wurde", "mail", "bitte"); it is left out unless no
# other word is present at all.
_COMMON_SHARE = 0.25


def _weighted_words(query: str, table: str, owner_sql: str, owner_params: list) -> list[tuple[str, float]]:
    """The query words that occur in this person's rows, each with its
    weight ln(N / n): rare words ("github", "kobra") count a lot, words
    in every third mail ("wurde", "erinnerst") next to nothing. Postgres'
    ts_rank knows no such thing, so until 2026-10-05 a mail matching
    "wurde" ranked like one matching "hansefit" (search test set)."""
    words = _query_words(query)
    if not words:
        return []
    try:
        with get_conn() as conn:
            total = conn.execute(f"SELECT count(*) AS n FROM {table} WHERE {owner_sql}", owner_params).fetchone()["n"] or 1
            counts = []
            for w in words:
                n = conn.execute(f"SELECT count(*) AS n FROM {table} WHERE {owner_sql} "
                                 f"AND {table}.search_tsv @@ to_tsquery('simple', ?)", (*owner_params, f"{w}:*")
                                 ).fetchone()["n"]
                counts.append((n, w))
    except Exception:  # noqa: BLE001
        return [(w, 1.0) for w in words]
    present = [(n, w) for n, w in counts if n > 0]
    if not present:
        return []
    import math
    rare = [(n, w) for n, w in present if n / total <= _COMMON_SHARE] or [min(present)]
    return [(w, round(math.log((total + 1) / (n + 1)) + 0.1, 3)) for n, w in rare]


def _rare_words_tsquery(query: str, table: str, owner_sql: str, owner_params: list) -> Optional[str]:
    """OR-tsquery of the words `_weighted_words` keeps (any may match;
    the weights order the rows)."""
    words = _weighted_words(query, table, owner_sql, owner_params)
    return " | ".join(f"{w}:*" for w, _ in words) or None


def _weight_sql(tsv: str, words: list[tuple[str, float]]) -> tuple[str, list]:
    """SQL summing the weight of every query word the row carries."""
    if not words:
        return "0", []
    sql = " + ".join(f"CASE WHEN {tsv} @@ to_tsquery('simple', ?) THEN {wt} ELSE 0 END" for _, wt in words)
    return f"({sql})", [f"{w}:*" for w, _ in words]


def _day_sql(col: str) -> str:
    """A stored time as its day 'YYYY-MM-DD': ISO strings, epoch seconds
    or plain dates, whatever the source wrote."""
    return (f"CASE WHEN {col}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN substr({col}::text, 1, 10) "
            f"WHEN {col}::text ~ '^\\d{{9,}}$' THEN to_char(to_timestamp({col}::text::bigint), 'YYYY-MM-DD') END")


def _date_filter(col: str, date_from: Optional[str], date_to: Optional[str]) -> tuple[str, list]:
    if not date_from and not date_to:
        return "TRUE", []
    day = _day_sql(col)
    return (f"({day} >= ? AND {day} <= ?)", [date_from or "0000-01-01", date_to or "9999-12-31"])


def _local(ts: Any) -> Any:
    """A timestamp as household local time with its offset. Stored times
    are UTC (epoch seconds, "+00:00" strings or naive "YYYY-MM-DD HH:MM:SS"
    from datetime('now')); the chat read "10:07" as local and said "heute
    Morgen um zehn" for 12:07 (rerun 2026-09-27). Plain dates stay dates."""
    from datetime import datetime as _dt, timezone as _tz
    if ts in (None, ""):
        return ts
    try:
        if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
            d = _dt.fromtimestamp(int(ts), _tz.utc)
        else:
            s_ = str(ts).strip()
            if len(s_) <= 10:
                return ts
            d = _dt.fromisoformat(s_.replace("Z", "+00:00").replace(" ", "T", 1))
            if d.tzinfo is None:
                d = d.replace(tzinfo=_tz.utc)
        from .push import _tz as _household_tz
        return d.astimezone(_household_tz()).isoformat(timespec="minutes")
    except (ValueError, OSError, TypeError):
        return ts


router = APIRouter(prefix="/api", tags=["search"])

PER_SOURCE_LIMIT = 8        # hits per source in the answer (the chat shows the model 5 after merging)
CANDIDATES = 12             # rows per branch (words, meaning) before they are fused
RRF_K = 20                  # reciprocal rank fusion constant, see _rrf
TOTAL_BUDGET_S = 4.0  # hard ceiling — Immich CLIP is the slow one


@dataclass
class Scope:
    """What the question narrows the search to besides its words: a
    span of days ("die Überweisung vom 2.7.", "letzte Woche") and
    whether the newest matching rows are wanted ("die letzten Mails von
    Beate"). The chat's prefetch reads it off the question
    (backend/agent/prefetch.py: time_scope)."""
    date_from: Optional[str] = None     # 'YYYY-MM-DD', inclusive
    date_to: Optional[str] = None
    recent: bool = False

    def __bool__(self) -> bool:
        return bool(self.date_from or self.date_to or self.recent)


_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/search")
async def universal_search(q: str = Query(..., min_length=2),
                            meaning: Optional[str] = None,
                            date_from: Optional[str] = None,
                            date_to: Optional[str] = None,
                            recent: bool = False,
                            user: dict = Depends(current_user)) -> dict[str, Any]:
    """Fan out a query across every channel the user has, return
    grouped results. Single-shot — caller decides how to render.
    `q` carries the words for the keyword branch; `meaning` (the whole
    question, when the caller has it) is what the meaning branch embeds:
    the embedder reads a sentence better than six bare words. `date_from`
    / `date_to` ('YYYY-MM-DD') and `recent` narrow and order, see Scope.
    Plain defaults, not Query(): the chat calls this function directly."""
    user_id = user["id"]
    role = user.get("role")
    scope = Scope(date_from=date_from if isinstance(date_from, str) and _ISO_DAY.match(date_from) else None,
                  date_to=date_to if isinstance(date_to, str) and _ISO_DAY.match(date_to) else None,
                  recent=recent is True)
    meaning = meaning if isinstance(meaning, str) else None
    # One embedding of the query serves every hybrid source.
    from . import search_index
    qvec = await asyncio.to_thread(search_index.embed_query, (meaning or q).strip() or q)

    tasks = {
        "email":      asyncio.to_thread(_search_email, q, user_id, qvec, scope),
        "whatsapp":   asyncio.to_thread(_search_whatsapp, q, user_id, qvec, scope),
        "paperless":  asyncio.to_thread(_search_paperless, q, user_id),
        "immich":     asyncio.to_thread(_search_immich, q, user_id),
        "calendar":   asyncio.to_thread(_search_calendar, q, user_id, role, qvec, scope),
        "tasks":      asyncio.to_thread(_search_tasks, q, user_id, role, qvec),
        "contacts":   asyncio.to_thread(_search_contacts, q, user_id, role, qvec),
        "recordings": asyncio.to_thread(_search_recordings, q, user_id, role, qvec),
        "drafts":     asyncio.to_thread(_search_drafts, q, user_id, qvec),
        "bank":       asyncio.to_thread(_search_bank, q, user_id, role, qvec, scope),
        "letters":    asyncio.to_thread(_search_letters, q, user_id, qvec),
        "pipelines":  asyncio.to_thread(_search_pipelines, q, user_id),
    }
    # A deadline so a slow source does not block the answer — but only
    # that source is left out. Until 2026-09-28 one late source (photos)
    # dropped every result, mail and documents too. The deadline follows
    # the machine's speed (speed.py).
    from . import speed
    limit = TOTAL_BUDGET_S * max(speed.factor("embed"), speed.factor("cpu"))
    futures = {name: asyncio.ensure_future(coro) for name, coro in tasks.items()}
    done, pending = await asyncio.wait(futures.values(), timeout=limit)
    for f in pending:
        f.cancel()
    results = []
    for name, f in futures.items():
        if f in done:
            exc = f.exception()
            results.append(exc if exc else f.result())
        else:
            log.info("search source %s missed the %.1f s deadline", name, limit)
            results.append([])

    out: dict[str, list] = {}
    total = 0
    for source_name, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            log.debug("search source %s raised: %s", source_name, result)
            out[source_name] = []
            continue
        # Cap to PER_SOURCE_LIMIT in case a helper returned more.
        hits = (result or [])[:PER_SOURCE_LIMIT]
        out[source_name] = hits
        total += len(hits)
    return {"query": q, "total": total, "results": out}


# ───────────────────────── email ────────────────────────────────────

def _rrf(ranked: list[tuple[list[dict[str, Any]], float]], key: Callable[[dict], Any] = lambda r: int(r["id"])
         ) -> list[dict[str, Any]]:
    """Reciprocal rank fusion: every list votes weight / (k + rank) for
    each of its rows, the sums order the result. k is 20, not the
    textbook 60: with a handful of rows per list, 60 lets a row that
    both lists mention halfway down beat the first row of either, and
    the one mail that carries the exact name must stay on top. Rows
    carry `_score`; a row's fields come from the list that saw it first
    (the meaning branch's `_snippet` survives when it ranked first)."""
    score: dict[Any, float] = {}
    rows: dict[Any, dict[str, Any]] = {}
    for hits, weight in ranked:
        for rank, r in enumerate(hits, 1):
            k = key(r)
            if k not in rows:
                rows[k] = dict(r)
            elif r.get("_snippet") and not rows[k].get("_snippet"):
                rows[k]["_snippet"] = r["_snippet"]
            score[k] = score.get(k, 0.0) + weight / (RRF_K + rank)
    out = []
    for k in sorted(score, key=lambda x: -score[x]):
        r = rows[k]
        r["_score"] = round(score[k], 4)
        out.append(r)
    return out


def _collapse(rows: list[dict[str, Any]], twin_key: Optional[Callable[[dict], Any]]) -> list[dict[str, Any]]:
    """The same thing several times (a reminder mail sent six times, ten
    "Kleingeld Plus" bookings) keeps one row, the best placed, and counts
    the rest in `_twins`; they used to fill every slot the model saw."""
    if not twin_key:
        return rows
    out: list[dict[str, Any]] = []
    index: dict[Any, dict[str, Any]] = {}
    for r in rows:
        k = twin_key(r)
        if k is None:
            out.append(r)
            continue
        if k in index:
            index[k]["_twins"] = index[k].get("_twins", 0) + 1
            continue
        index[k] = r
        out.append(r)
    return out


def _hybrid(*, source: str, table: str, columns: str, visible: tuple[str, list],
            keyword: Optional[tuple[str, list]], order: str, qvec: Optional[str],
            order_params: Optional[list] = None, date_key: Optional[str] = None,
            scope: Optional[Scope] = None, twin_key: Optional[Callable[[dict], Any]] = None,
            weights: tuple[float, float] = (1.0, 1.0), recent_order: Optional[str] = None,
            recent_params: Optional[list] = None) -> list[dict[str, Any]]:
    """Rows found by their words and rows found by meaning, fused into
    one list, for one table. `visible` and `keyword` are (sql, params)
    over the table's own name. Rows found by meaning carry `_snippet`,
    the piece of text that matched.

    Until 2026-10-05 the two lists were interleaved (word 1, meaning 1,
    word 2, …) and the chat showed three rows: only the single best hit
    by meaning could ever reach the model, the second best (distance
    0.25, as good as it gets) never did. Now both lists vote (_rrf) and
    twins are collapsed (_collapse)."""
    vis_sql, vis_params = visible
    if scope and date_key and (scope.date_from or scope.date_to):
        d_sql, d_params = _date_filter(f"{table}.{date_key}", scope.date_from, scope.date_to)
        vis_sql, vis_params = f"({vis_sql}) AND {d_sql}", [*vis_params, *d_params]
    if scope and scope.recent and date_key:
        # "die letzten Mails von Beate Mayer": rows whose sender or title
        # carries the words first (`recent_order`, else just the date),
        # newest first — ordered by date alone, every newsletter that
        # mentions a Mayer came before her mails.
        head = f"{recent_order} DESC, " if recent_order else ""
        order, order_params = f"{head}{table}.{date_key} DESC NULLS LAST", list(recent_params or [])
        weights = (weights[0] * 1.2, weights[1] * 0.8)
    rows: list[dict[str, Any]] = []
    with get_conn() as conn:
        if keyword:
            kw_sql, kw_params = keyword
            try:
                for r in conn.execute(
                    f"SELECT {columns} FROM {table} WHERE ({kw_sql}) AND ({vis_sql}) "
                    f"ORDER BY {order} LIMIT ?",
                    (*kw_params, *vis_params, *(order_params or []), CANDIDATES),
                ).fetchall():
                    rows.append(dict(r))
            except Exception as exc:  # noqa: BLE001
                log.warning("universal-search %s keyword branch failed: %s", source, exc)
        sem_rows: list[dict[str, Any]] = []
        if qvec:
            from . import search_index
            limit = search_index.max_distance()
            seen: set[int] = set()
            try:
                for r in conn.execute(
                    f"SELECT {columns}, sc.text AS _snippet, (sc.embedding <=> ?::vector) AS _distance "
                    f"FROM search_chunks sc JOIN {table} ON {table}.id = sc.row_id "
                    f"WHERE sc.source = ? AND sc.model = ? AND sc.embedding IS NOT NULL AND ({vis_sql}) "
                    f"ORDER BY _distance LIMIT ?",
                    (qvec, source, search_index.model_tag(), *vis_params, CANDIDATES * 4),
                ).fetchall():
                    if r["_distance"] is None or r["_distance"] > limit:
                        break
                    if int(r["id"]) in seen:
                        continue
                    sem_rows.append(dict(r)); seen.add(int(r["id"]))
                    if len(sem_rows) >= CANDIDATES:
                        break
            except Exception as exc:  # noqa: BLE001
                log.warning("universal-search %s semantic branch failed: %s", source, exc)
    if sem_rows and date_key:
        # Hits about as close as each other: the newer one first ("was hab
        # ich für Claude bezahlt" showed May's receipt, not September's).
        buckets: dict[int, list] = {}
        for r in sem_rows:
            buckets.setdefault(round(float(r.get("_distance") or 1) / 0.04), []).append(r)
        sem_rows = [r for b in sorted(buckets) for r in sorted(buckets[b], key=lambda x: str(x.get(date_key) or ""), reverse=True)]
    fused = _rrf([(rows, weights[0]), (sem_rows, weights[1])])
    return _collapse(fused, twin_key)[:PER_SOURCE_LIMIT]


_RE_PREFIX = re.compile(r"^(?:(?:re|aw|wg|fwd?|fw|antw|erinnerung|reminder)\s*:\s*)+", re.I)


def _norm_subject(s: Any) -> str:
    """A subject without reply prefixes, numbers and dates — "Erinnerung:
    Mit Docusign abschließen: 2026-08-31 …" six times is one thing."""
    t = _RE_PREFIX.sub("", str(s or "").strip().lower())
    t = re.sub(r"\d+", "#", t)
    return re.sub(r"\s+", " ", t).strip()


def _email_twin(r: dict) -> Any:
    subj = _norm_subject(r.get("subject"))
    body = re.sub(r"\W+", " ", str(r.get("snippet") or "").lower())[:60]
    return (str(r.get("from_email") or "").lower(), subj, body) if subj else None


def _bank_twin(r: dict) -> Any:
    return (str(r.get("counterparty") or "").lower(), str(r.get("amount") or ""),
            re.sub(r"\d+", "#", str(r.get("purpose") or "").lower())[:40])


def _wa_twin(r: dict) -> Any:
    text = re.sub(r"\s+", " ", str(r.get("text") or r.get("transcript") or "").strip().lower())
    return (r.get("chat_jid"), text[:80]) if len(text) > 20 else None


def _like(q: str, *cols: str) -> tuple[str, list]:
    """Every word of the query in any of the columns."""
    words = [w for w in q.lower().split() if w][:6] or [q.lower()]
    hay = " || ' ' || ".join(f"LOWER(COALESCE({c}, ''))" for c in cols)
    return " AND ".join(f"({hay}) LIKE ?" for _ in words), [f"%{w}%" for w in words]


def _tsquery(words: list[tuple[str, float]], recent: bool) -> Optional[str]:
    """OR of the weighted words; when the newest rows are wanted ("die
    letzten Mails von Beate Mayer") only the rarest word decides what
    matches, the date orders — an OR over "letzten | beate | mayer" by
    date is every newsletter of the week."""
    if not words:
        return None
    if recent:
        return f"{max(words, key=lambda x: x[1])[0]}:*"
    return " | ".join(f"{w}:*" for w, _ in words)


def _twins_field(r: dict) -> dict[str, Any]:
    return {"duplicates": r["_twins"]} if r.get("_twins") else {}


def _search_email(q: str, user_id: str, qvec: Optional[str] = None,
                  scope: Optional[Scope] = None) -> list[dict[str, Any]]:
    """tsvector over email_messages.search_tsv (words weighted by rarity,
    sender and subject count extra, newsletters less), fused with the
    semantic index."""
    words = _weighted_words(q, "email_messages", "email_messages.owner_user_id = ?", [user_id])
    tsq = _tsquery(words, recent=bool(scope and scope.recent))
    weight_sql, weight_params = _weight_sql("email_messages.search_tsv", words)
    rows = _hybrid(
        source="email", table="email_messages",
        columns="email_messages.id, subject, from_name, from_email, snippet, date_received",
        visible=("email_messages.owner_user_id = ?", [user_id]),
        keyword=("email_messages.search_tsv @@ to_tsquery('simple', ?)", [tsq]) if tsq else None,
        # relevance first: rare words, words in sender/subject, no newsletters; then newest
        order=(f"({weight_sql} + 0.5 * ts_rank(email_messages.search_tsv, to_tsquery('simple', ?::text)) "
               " + CASE WHEN LOWER(COALESCE(subject,'') || ' ' || COALESCE(from_name,'') || ' ' "
               "   || COALESCE(from_email,'')) ~ ?::text THEN 1.5 ELSE 0 END "
               " - CASE WHEN COALESCE(email_messages.category,'') IN ('newsletter','spam') THEN 1.0 ELSE 0 END"
               ") DESC, date_received DESC NULLS LAST") if tsq else "date_received DESC NULLS LAST",
        order_params=[*weight_params, tsq, _words_regex(q) or "(?!)"] if tsq else None,
        qvec=qvec, date_key="date_received", scope=scope, twin_key=_email_twin,
        recent_order=("(CASE WHEN LOWER(COALESCE(from_name,'') || ' ' || COALESCE(from_email,'')) ~ ?::text THEN 2 "
                      " WHEN LOWER(COALESCE(subject,'')) ~ ?::text THEN 1 ELSE 0 END)"),
        recent_params=[_words_regex(q) or "(?!)"] * 2,
    )
    return [{
        "source":      "email",
        "id":          r["id"],
        "title":       r["subject"] or "(no subject)",
        "subtitle":    r["from_name"] or r["from_email"],
        "snippet":     (r.get("_snippet") or r["snippet"] or "")[:200],
        "timestamp":   _local(r["date_received"]),
        **_twins_field(r),
        # Mig 124: react app lives at /r/email; ?msg=<id> is the deep-
        # link the EmailApp parses on mount.
        "navigate_to": f"/r/email?msg={r['id']}",
    } for r in rows]


# ───────────────────────── WhatsApp ─────────────────────────────────

def _search_whatsapp(q: str, user_id: str, qvec: Optional[str] = None,
                     scope: Optional[Scope] = None) -> list[dict[str, Any]]:
    """tsvector over wa_messages.search_tsv (words weighted by rarity, the
    chat's and sender's name count extra), fused with the semantic index."""
    words = _weighted_words(q, "wa_messages", "wa_messages.owner_user_id = ?", [user_id])
    tsq = _tsquery(words, recent=bool(scope and scope.recent))
    weight_sql, weight_params = _weight_sql("wa_messages.search_tsv", words)
    rows = _hybrid(
        source="whatsapp", table="wa_messages",
        columns="wa_messages.id, wa_messages.chat_jid, wa_messages.text, wa_messages.transcript, "
                "wa_messages.push_name, wa_messages.from_me, wa_messages.timestamp, "
                "(SELECT c.name FROM wa_chats c WHERE c.jid = wa_messages.chat_jid LIMIT 1) AS chat_name",
        visible=("wa_messages.owner_user_id = ?", [user_id]),
        keyword=("wa_messages.search_tsv @@ to_tsquery('simple', ?)", [tsq]) if tsq else None,
        order=(f"({weight_sql} + 0.5 * ts_rank(wa_messages.search_tsv, to_tsquery('simple', ?::text))"
               " + CASE WHEN LOWER(COALESCE(wa_messages.push_name,'') || ' ' || COALESCE("
               "   (SELECT c.name FROM wa_chats c WHERE c.jid = wa_messages.chat_jid LIMIT 1),'')) ~ ?::text "
               "   THEN 1.0 ELSE 0 END) DESC, wa_messages.timestamp DESC"
               if tsq else "wa_messages.timestamp DESC"),
        order_params=[*weight_params, tsq, _words_regex(q) or "(?!)"] if tsq else None,
        qvec=qvec, date_key="timestamp", scope=scope, twin_key=_wa_twin,
        recent_order=("(CASE WHEN LOWER(COALESCE(wa_messages.push_name,'') || ' ' || COALESCE("
                      "(SELECT c.name FROM wa_chats c WHERE c.jid = wa_messages.chat_jid LIMIT 1),'')) ~ ?::text "
                      "THEN 1 ELSE 0 END)"),
        recent_params=[_words_regex(q) or "(?!)"],
    )
    rows = _wa_values(q, user_id) + [r for r in rows]
    out, seen_ids = [], set()
    for r in rows:
        if r["id"] in seen_ids:
            continue
        seen_ids.add(r["id"])
        text = r["text"] or r["transcript"] or ""
        chat = r["chat_name"] or (r["chat_jid"].split("@")[0] if r["chat_jid"] else "?")
        out.append({
            "source":      "whatsapp",
            "id":          r["id"],
            "title":       chat,
            "subtitle":    r["push_name"] or "",
            # who wrote it: "DE85 … von Mama" was the person's own message
            # "you" read as Yorik; "the user" is the person asking (2026-09-28)
            "who":         "the user" if r.get("from_me") else (r["push_name"] or chat),
            # checked here, so the model does not guess ("nur 20 Zeichen" for a
            # correct 22-character IBAN, 2026-09-28)
            **({"iban_check": "valid IBAN, check digits correct"} if r.get("iban_ok") else {}),
            "snippet":     text[:200],
            "timestamp":   _local(r["timestamp"]),
            **_twins_field(r),
            "navigate_to": f"/r/whatsapp?chat={r['chat_jid']}",
            # where it lies, so the chat can read the messages around it
            "chat_jid":    r["chat_jid"],
        })
    return out


_ASKS_ACCOUNT = re.compile(r"\b(iban|kontonummer|konto(?:daten)?|bankverbindung|bankdaten|account number|bank details)\b", re.I)
_IBAN_SQL = r"[A-Z]{2}[0-9]{2}( ?[A-Z0-9]{4}){3,7}"


def _wa_values(q: str, user_id: str) -> list[dict[str, Any]]:
    """A question for an account number gets every distinct IBAN of the
    chats it names, each where it first appeared — the five newest
    messages were the person's own forwards, and Mama's own IBAN from
    January never came up (chat test #18, 2026-09-28)."""
    if not _ASKS_ACCOUNT.search(q or ""):
        return []
    names = [w for w in re.findall(r"\w+", q.lower())
             if len(w) > 2 and not _ASKS_ACCOUNT.fullmatch(w)]
    who = " OR ".join("(LOWER(COALESCE(c.name, '')) LIKE ? OR LOWER(COALESCE(m.push_name, '')) LIKE ?)" for _ in names)
    params: list[Any] = [user_id]
    for w in names:
        params += [f"%{w}%", f"%{w}%"]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (k.iban) k.id, k.chat_jid, k.text, k.transcript, k.push_name, k.from_me, k.timestamp, k.chat_name "
            "FROM (SELECT m.id, m.chat_jid, m.text, m.transcript, m.push_name, m.from_me, m.timestamp, c.name AS chat_name, "
            "             UPPER(REPLACE(substring(m.text from ?), ' ', '')) AS iban "
            "        FROM wa_messages m LEFT JOIN wa_chats c ON c.jid = m.chat_jid AND c.owner_user_id = m.owner_user_id "
            f"       WHERE m.owner_user_id = ? AND m.text ~ ? {('AND (' + who + ')') if names else ''}) k "
            "ORDER BY k.iban, k.timestamp ASC",
            (_IBAN_SQL, params[0], _IBAN_SQL, *params[1:])).fetchall()
    real = [{**dict(r), "iban_ok": True} for r in rows if _iban_ok(r["text"] or "")]   # a link's code is no IBAN
    return sorted(real, key=lambda r: r["timestamp"] or 0)[:PER_SOURCE_LIMIT]


def _iban_ok(text: str) -> bool:
    """The text carries an IBAN whose check digits are right (mod 97)."""
    for m in re.finditer(r"\b[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b", text):
        raw = m.group(0).replace(" ", "")
        moved = raw[4:] + raw[:4]
        digits = "".join(str(int(ch, 36)) for ch in moved)
        if 15 <= len(raw) <= 34 and int(digits) % 97 == 1:
            return True
    return False


# ───────────────────────── Paperless ────────────────────────────────

# Cosine-distance threshold for surfacing paperless hits in
# universal search. paperless_ingest.search uses nearest-neighbour
# without a cutoff, so even when nothing in the corpus is actually
# related to the query it returns the K closest documents. For a
# user-facing search panel that's a noise factory — empirically the
# closest match for unrelated queries lands at distance 0.7-0.9.
# 0.55 keeps the "broadly the same topic" matches (~50° angle or
# cleaner) and drops everything weaker.
#
# Other callers of paperless_ingest.search (agent RAG context,
# explicit /docs search) still see all hits — only the universal
# /api/search panel gates by relevance.
_PAPERLESS_MAX_DISTANCE = 0.55


def _search_paperless(q: str, user_id: str) -> list[dict[str, Any]]:
    """Semantic search through paperless_ingest with the user's per-user
    Paperless token (so Anna's search only sees Anna's docs)."""
    try:
        from . import paperless_ingest
        from .external_users import get_user_paperless_creds
        creds = get_user_paperless_creds(user_id)
        sem = paperless_ingest.search(q, k=PER_SOURCE_LIMIT, creds_override=creds)
    except Exception:
        sem = []
    try:
        limit = paperless_ingest.semantic_max_distance()
    except Exception:  # noqa: BLE001
        limit = _PAPERLESS_MAX_DISTANCE
    sem = [h for h in (sem or [])
           if h.get("distance") is not None and h["distance"] <= limit]
    # Keyword leg: Paperless' own full-text search, as the person (their
    # token, so their permissions). Any word may match — "github" found
    # nothing by meaning at 0.55 and the chat concluded "no GitHub
    # payments" (chat test 2026-09-27).
    fts: list[dict[str, Any]] = []
    words = [w for w in re.findall(r"\w+", q) if len(w) > 2][:6]
    if words and creds:
        try:
            fts = paperless_ingest.search_fts(" OR ".join(words), k=PER_SOURCE_LIMIT, creds_override=creds) or []
        except Exception:  # noqa: BLE001
            fts = []
    sem_ids = [h.get("paperless_doc_id") for h in sem]
    both = [h for h in fts if h.get("paperless_doc_id") in sem_ids]
    relevant, seen = [], set()
    # Meaning before a bare word match since the documents share the
    # multilingual index: "rechnung" put "test-rechnung" above the netcup
    # invoice (document test set 2026-09-27).
    for h in both + sem + fts:
        did = h.get("paperless_doc_id")
        if did in seen:
            continue
        seen.add(did)
        # prefer the semantic leg's chunk as snippet (a real passage)
        relevant.append(next((x for x in sem if x.get("paperless_doc_id") == did), h))
    return [{
        "source":      "paperless",
        "id":          h.get("paperless_doc_id"),
        "title":       h.get("doc_title") or "(untitled doc)",
        "subtitle":    h.get("correspondent") or "",
        "snippet":     (h.get("text") or "")[:200],
        "timestamp":   h.get("doc_date"),
        # In-app links only: Paperless' own doc_url/preview_url point at
        # http://localhost:8010, dead on a phone (chat test 2026-09-26).
        "navigate_to": (f"/r/documents?doc={h['paperless_doc_id']}&source=paperless"
                        if h.get("paperless_doc_id") else "/r/documents"),
    } for h in relevant]


# ───────────────────────── Immich ───────────────────────────────────

def _search_immich(q: str, user_id: str) -> list[dict[str, Any]]:
    try:
        from .connectors.immich import immich
        from .external_users import get_user_immich_creds
        creds = get_user_immich_creds(user_id)
        result = immich(op="search", query=q, take_count=PER_SOURCE_LIMIT,
                         creds_override=creds)
        photos = result.get("photos") or []
    except Exception:
        return []
    return [{
        "source":      "immich",
        "id":          p.get("id"),
        "title":       p.get("original_name") or "Photo",
        "subtitle":    "",
        "snippet":     "",
        "timestamp":   _local(p.get("taken_at")),
        "navigate_to": f"/r/photos?asset={p['id']}" if p.get("id") else "/r/photos",
        "thumbnail_url": p.get("thumbnail_url"),
    } for p in photos]


# ───────────────────────── calendar ─────────────────────────────────

def _search_calendar(q: str, user_id: str, role: Optional[str] = None,
                     qvec: Optional[str] = None, scope: Optional[Scope] = None) -> list[dict[str, Any]]:
    """Events the person can see in the calendar, nothing else: the
    calendar's own filter, and another person's private event (shown
    there as "Busy") is not found at all."""
    from . import calendars as _cal
    vis_sql, vis_params = _cal.visible_event_filter(user_id, role or "")
    rows = _hybrid(
        source="events", table="events",
        columns="events.id, title, starts_at, ends_at, person, notes, location",
        visible=(f"{vis_sql} AND (events.visibility IS DISTINCT FROM 'private' OR events.owner_user_id = ?)",
                 [*vis_params, user_id]),
        keyword=_like(q, "events.title", "events.notes", "events.person", "events.location"),
        order="starts_at DESC", qvec=qvec, date_key="starts_at", scope=scope,
    )
    return [{
        "source":      "calendar",
        "id":          r["id"],
        "title":       r["title"],
        "subtitle":    r["person"] or r["location"] or "",
        "snippet":     (r["notes"] or "")[:200],
        "timestamp":   r["starts_at"],
        "navigate_to": f"/r/calendar?event={r['id']}",
    } for r in rows]


# ───────────────────────── tasks ────────────────────────────────────

def _search_tasks(q: str, user_id: str, role: Optional[str] = None,
                  qvec: Optional[str] = None) -> list[dict[str, Any]]:
    from . import spaces as _sp
    rows = _hybrid(
        source="tasks", table="tasks",
        columns="tasks.id, title, notes, due_date, done, person",
        visible=_sp.row_filter(user_id, role, "tasks"),
        keyword=_like(q, "tasks.title", "tasks.notes", "tasks.person"),
        order="done ASC, COALESCE(due_date, '9999') ASC, tasks.id DESC", qvec=qvec,
    )
    return [{
        "source":      "tasks",
        "id":          r["id"],
        "title":       r["title"],
        "subtitle":    ("done" if r["done"] else "open") + (f" · {r['person']}" if r["person"] else ""),
        "snippet":     (r["notes"] or "")[:200],
        "timestamp":   r["due_date"],
        "navigate_to": f"/r/tasks?task={r['id']}",
    } for r in rows]


# ───────────────────────── contacts ─────────────────────────────────

def _search_contacts(q: str, user_id: str, role: Optional[str] = None,
                     qvec: Optional[str] = None) -> list[dict[str, Any]]:
    from . import spaces as _sp
    vis_sql, vis_params = _sp.row_filter(user_id, role, "contacts")
    rows = _hybrid(
        source="contacts", table="contacts",
        columns="contacts.id, display_name, relation, role, notes, last_interaction_at",
        visible=(f"{vis_sql} AND contacts.status = 'active' AND contacts.merged_into_id IS NULL", vis_params),
        keyword=_like(q, "contacts.display_name", "contacts.aliases", "contacts.legal_name",
                      "contacts.notes", "contacts.tags"),
        order="COALESCE(last_interaction_at, '') DESC, contacts.id DESC", qvec=qvec,
    )
    return [{
        "source":      "contacts",
        "id":          r["id"],
        "title":       r["display_name"],
        "subtitle":    r["relation"] or r["role"] or "",
        "snippet":     (r["notes"] or "")[:200],
        "timestamp":   _local(r["last_interaction_at"]),
        "navigate_to": f"/r/contacts?contact={r['id']}",
    } for r in rows]


# ───────────────────────── recordings ───────────────────────────────

def _search_recordings(q: str, user_id: str, role: Optional[str] = None,
                       qvec: Optional[str] = None) -> list[dict[str, Any]]:
    """Title, report and transcript of the recordings the person was
    part of. The keyword branch reads the transcript as well."""
    rows = _recordings_rows(q, user_id, role, qvec)
    return [{
        "source":      "recordings",
        "id":          r["id"],
        "title":       r["title"] or "Recording",
        "subtitle":    r["kind"] or "",
        "snippet":     (r.get("_snippet") or r.get("seg_hit") or "")[:200],
        "timestamp":   _local(r["started_at"]),
        "navigate_to": f"/r/recordings/{r['id']}",
    } for r in rows]


def _recordings_rows(q: str, user_id: str, role: Optional[str], qvec: Optional[str]) -> list[dict[str, Any]]:
    """Any query word may match title, report or transcript; rows with
    more words in the title and report come first. All words had to
    match, so "besprochen regeln yorik" never found "Regeln für Yorik"
    (rerun 2026-09-27)."""
    from . import spaces as _sp
    words = [w.lower() for w in re.findall(r"\w+", q or "") if len(w) > 3][:6] or [(q or "").lower()]
    title_hit = " + ".join("CASE WHEN LOWER(COALESCE(recordings.title,'')) LIKE ? THEN 2 ELSE 0 END" for _ in words)
    report_hit = " + ".join("CASE WHEN LOWER(COALESCE(recordings.report_json,'')) LIKE ? THEN 1 ELSE 0 END" for _ in words)
    likes = [f"%{w}%" for w in words]
    any_field = " OR ".join("LOWER(COALESCE(recordings.title,'') || ' ' || COALESCE(recordings.report_json,'')) LIKE ?"
                            for _ in words)
    seg_any = " OR ".join("LOWER(s.text) LIKE ?" for _ in words)
    rows = _hybrid(
        source="recordings", table="recordings",
        columns="recordings.id, title, kind, started_at",
        visible=_sp.row_filter(user_id, role, "recordings"),
        keyword=(f"(({any_field}) OR EXISTS (SELECT 1 FROM recording_segments s "
                 f"WHERE s.recording_id = recordings.id AND ({seg_any})))", [*likes, *likes]),
        order=f"({title_hit} + {report_hit}) DESC, started_at DESC",
        order_params=[*likes, *likes],
        qvec=qvec, date_key="started_at",
    )
    # The sentence that matched, for rows found by keyword.
    with get_conn() as conn:
        for r in rows:
            if r.get("_snippet"):
                continue
            hit = conn.execute(
                f"SELECT s.text FROM recording_segments s WHERE s.recording_id = ? AND ({seg_any}) "
                f"ORDER BY s.seq LIMIT 1", (r["id"], *likes)).fetchone()
            r["seg_hit"] = hit["text"] if hit else ""
    return rows


# ───────────────────────── drafts ───────────────────────────────────

def _search_drafts(q: str, user_id: str, qvec: Optional[str] = None) -> list[dict[str, Any]]:
    rows = _hybrid(
        source="drafts", table="compose_drafts",
        columns="compose_drafts.id, subject, recipient, kind, updated_at",
        visible=("compose_drafts.user_id = ?", [user_id]),
        keyword=_like(q, "compose_drafts.subject", "compose_drafts.recipient", "compose_drafts.body_html"),
        order="updated_at DESC", qvec=qvec,
    )
    return [{
        "source":      "drafts",
        "id":          r["id"],
        "title":       r["subject"] or "(no subject)",
        "subtitle":    " · ".join(x for x in (r["kind"], r["recipient"]) if x),
        "snippet":     (r.get("_snippet") or "")[:200],
        "timestamp":   _local(r["updated_at"]),
        "navigate_to": f"/r/compose?draft_id={r['id']}",
    } for r in rows]


# ───────────────────────── bank ─────────────────────────────────────

def _search_bank(q: str, user_id: str, role: Optional[str] = None,
                 qvec: Optional[str] = None, scope: Optional[Scope] = None) -> list[dict[str, Any]]:
    """Bookings on the accounts the person may see (own and shared),
    the same rule as the finance skills."""
    from . import spaces as _sp
    frag, params = _sp.row_filter(user_id, role, "bank_accounts", table_alias="a")
    rows = _hybrid(
        source="bank", table="bank_transactions",
        columns="bank_transactions.id, booking_date, amount, counterparty, purpose, category",
        visible=(f"bank_transactions.account_id IN (SELECT a.id FROM bank_accounts a WHERE {frag})", list(params)),
        keyword=_like(q, "bank_transactions.counterparty", "bank_transactions.purpose", "bank_transactions.category"),
        order="booking_date DESC", qvec=qvec, date_key="booking_date", scope=scope, twin_key=_bank_twin,
        recent_order="(CASE WHEN LOWER(COALESCE(counterparty,'')) ~ ?::text THEN 1 ELSE 0 END)",
        recent_params=[_words_regex(q) or "(?!)"],
    )
    return [{
        "source":      "bank",
        "id":          r["id"],
        "title":       r["counterparty"] or "(ohne Empfänger)",
        "subtitle":    f"{r['booking_date']} · {r['amount']} EUR",
        "snippet":     (r["purpose"] or "")[:200],
        "timestamp":   r["booking_date"],
        **_twins_field(r),
        "navigate_to": "/r/finance",
    } for r in rows]


# ───────────────────────── letters (Schreiben) ──────────────────────

def _search_letters(q: str, user_id: str, qvec: Optional[str] = None) -> list[dict[str, Any]]:
    rows = _hybrid(
        source="letters", table="written_documents",
        columns="written_documents.id, kind, status, title, recipient, updated_at",
        visible=("written_documents.user_id = ?", [user_id]),
        keyword=_like(q, "written_documents.title", "written_documents.recipient", "written_documents.content"),
        order="updated_at DESC", qvec=qvec,
    )
    out = []
    for r in rows:
        try:
            name = (json.loads(r["recipient"] or "{}") or {}).get("name") or ""
        except ValueError:
            name = ""
        out.append({
            "source":      "letters",
            "id":          r["id"],
            "title":       r["title"] or "(ohne Titel)",
            "subtitle":    " · ".join(x for x in (r["kind"], r["status"], name) if x),
            "snippet":     (r.get("_snippet") or "")[:200],
            "timestamp":   _local(r["updated_at"]),
            "navigate_to": f"/r/write?id={r['id']}",
        })
    return out


# ───────────────────────── pipelines ────────────────────────────────

_PIPELINE_STATES = {"entwurf": "Entwurf", "laeuft": "läuft", "pausiert": "pausiert",
                    "erledigt": "erledigt", "abgebrochen": "abgebrochen"}


def _search_pipelines(q: str, user_id: str) -> list[dict[str, Any]]:
    """The person's follow-ups whose title, goal or original mail matches
    a query word — "die GoHighLevel-Sache, wo ich nachfassen wollte" found
    the mail but not the pipeline already following it (rerun 2026-09-27)."""
    words = [w.lower() for w in re.findall(r"\w+", q or "") if len(w) > 3]
    if not words:
        return []
    try:
        from .pipelines import store
        rows = store.list_for(str(user_id))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in rows:
        origin = p.get("origin") or {}
        hay = " ".join(str(x) for x in (p.get("title"), p.get("goal"), origin.get("subject"),
                                        " ".join(origin.get("to") or []))).lower()
        if any(w in hay for w in words):
            out.append({
                "source":      "pipelines",
                "id":          p["id"],
                "title":       p.get("title") or "Pipeline",
                "subtitle":    _PIPELINE_STATES.get(p.get("state") or "", p.get("state") or ""),
                "snippet":     (p.get("goal") or "")[:200],
                "timestamp":   _local(p.get("updated_at")),
                "navigate_to": f"/r/pipelines/{p['id']}",
            })
    return out[:PER_SOURCE_LIMIT]


# ───────────────────────── settings: search by meaning ──────────────

def _require_admin(user: dict) -> None:
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="role required: admin")


@router.get("/search/index")
def search_index_status(user: dict = Depends(current_user)) -> dict[str, Any]:
    """Settings → Embeddings: is search by meaning on, with which
    model, and how far is the index."""
    _require_admin(user)
    from . import search_index as si
    indexed, totals = si.stats(), si.totals()
    return {
        "enabled": si.enabled(),
        "embedder": "service" if si.use_service() else "bundled",
        "model": si.model_tag(),
        "service": {"configured": bool(si.EMBED_URL), "model": si.EMBED_MODEL,
                    "reachable": si.service_reachable()},
        "sources": [{"source": name, "indexed": indexed.get(name, 0), "total": totals.get(name, 0)}
                    for name in si.SOURCES],
    }


from pydantic import BaseModel  # noqa: E402


class _SearchIndexIn(BaseModel):
    enabled: Optional[bool] = None
    embedder: Optional[str] = None      # 'service' | 'bundled'


@router.put("/search/index")
def search_index_set(body: _SearchIndexIn, user: dict = Depends(current_user)) -> dict[str, Any]:
    _require_admin(user)
    from . import search_index as si
    from .household_settings import set_setting
    if body.enabled is not None:
        set_setting(si.SETTING_ENABLED, "1" if body.enabled else "0", updated_by_user_id=user["id"])
    if body.embedder is not None:
        if body.embedder not in ("service", "bundled"):
            raise HTTPException(status_code=400, detail="embedder must be 'service' or 'bundled'")
        if body.embedder == "service" and not si.EMBED_URL:
            raise HTTPException(status_code=400, detail="no embedding service is installed "
                                "(scripts/install-search-embedder.sh)")
        set_setting(si.SETTING_EMBEDDER, body.embedder, updated_by_user_id=user["id"])
    si.wake()
    return search_index_status(user)


@router.post("/search/index/rebuild")
def search_index_rebuild(user: dict = Depends(current_user)) -> dict[str, Any]:
    _require_admin(user)
    from . import search_index as si
    dropped = si.clear()
    si.wake()
    return {"ok": True, "dropped": dropped}
