"""Typo-tolerant names in the search.

"rivertie", "docusing", "ideenschmide", "gogle" found nothing (search
test set 2026-10-05); the model's other wordings then invented a
"Rivertie GmbH". A query word that occurs nowhere in the person's mail
or WhatsApp is compared (pg_trgm) with the words of their senders,
subjects and chat names — table `search_vocab`, migration 172 — and the
closest one replaces it when it is close enough. Only words with no hit
at all are touched, so a correction can never cost a match; the chat is
told what was corrected (`corrected` in the search result) so it can say
"meintest du Riverty?".

The vocabulary is per person (their own rows only — a word from someone
else's mail must not show up as a suggestion) and rebuilt by the index
sweep at most once an hour.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

from .database import get_conn

log = logging.getLogger("yorik.search_vocab")

MIN_SIMILARITY = 0.5        # pg_trgm similarity; "rivertie"/"riverty" ≈ 0.6, "gogle"/"google" ≈ 0.55
MIN_LEN = 4                 # shorter words are too easily "corrected" to something else
REFRESH_S = 3600
_last_refresh = 0.0
_available: Optional[bool] = None

# one row per (owner, word) from the fields a person would name
_SOURCES = [
    ("email_messages", "owner_user_id", "coalesce(from_name,'') || ' ' || coalesce(subject,'')", "TRUE"),
    ("wa_messages", "owner_user_id",
     "coalesce(push_name,'') || ' ' || coalesce((SELECT c.name FROM wa_chats c WHERE c.jid = t.chat_jid "
     "AND c.owner_user_id = t.owner_user_id LIMIT 1),'')", "TRUE"),
]
_WORD = re.compile(r"^[a-zäöüß][a-zäöüß0-9\-]{3,}$")


def available(conn) -> bool:
    """pg_trgm and the table are there (checked once per process)."""
    global _available
    if _available is None:
        try:
            ext = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'").fetchone()
            tbl = conn.execute("SELECT to_regclass('search_vocab') AS t").fetchone()
            _available = bool(ext) and bool(tbl and tbl["t"])
            if not _available:
                log.info("search vocab off: pg_trgm or search_vocab missing")
        except Exception as exc:  # noqa: BLE001
            log.info("search vocab off: %s", exc)
            _available = False
    return _available


def refresh(force: bool = False) -> int:
    """Rebuild the vocabulary when it is older than REFRESH_S. Returns
    the number of words, -1 when nothing was done."""
    global _last_refresh
    if not force and time.time() - _last_refresh < REFRESH_S:
        return -1
    with get_conn() as conn:
        if not available(conn):
            return -1
        conn.execute("DELETE FROM search_vocab")
        total = 0
        for table, owner, expr, where in _SOURCES:
            res = conn.execute(
                f"INSERT INTO search_vocab (owner_user_id, word, n) "
                f"SELECT {owner}, w, count(*) FROM ("
                f"  SELECT t.{owner}, regexp_split_to_table(lower({expr}), '[^[:alnum:]äöüß\\-]+') AS w "
                f"  FROM {table} t WHERE {where} AND t.{owner} IS NOT NULL) x "
                f"WHERE length(w) >= ? AND w ~ '^[a-zäöüß]' "
                f"GROUP BY {owner}, w "
                f"ON CONFLICT (owner_user_id, word) DO UPDATE SET n = search_vocab.n + EXCLUDED.n",
                (MIN_LEN,))
            total += res.rowcount if res.rowcount and res.rowcount > 0 else 0
        conn.commit()
    _last_refresh = time.time()
    log.info("search vocab rebuilt: %d word(s)", total)
    return total


def _has_hits(conn, word: str, user_id: Any) -> bool:
    for table in ("email_messages", "wa_messages"):
        r = conn.execute(f"SELECT 1 FROM {table} WHERE owner_user_id = ? AND search_tsv @@ to_tsquery('simple', ?) LIMIT 1",
                         (user_id, f"{word}:*")).fetchone()
        if r:
            return True
    return False


def correct(words: List[str], user_id: Any) -> Dict[str, str]:
    """{typo: word} for the query words that hit nothing and have a
    close enough name in this person's vocabulary."""
    out: Dict[str, str] = {}
    cands = [w for w in words if _WORD.match(w.lower())]
    if not cands:
        return out
    try:
        with get_conn() as conn:
            if not available(conn):
                return out
            for w in cands:
                lw = w.lower()
                if _has_hits(conn, lw, user_id):
                    continue
                r = conn.execute(
                    "SELECT word, similarity(word, ?) AS s, n FROM search_vocab "
                    "WHERE owner_user_id = ? AND word % ? AND abs(length(word) - ?) <= 3 "
                    "ORDER BY s DESC, n DESC LIMIT 1", (lw, user_id, lw, len(lw))).fetchone()
                if r and float(r["s"]) >= MIN_SIMILARITY and r["word"] != lw:
                    out[w] = r["word"]
    except Exception as exc:  # noqa: BLE001 — a correction is a bonus, never a failure
        log.info("search vocab correction skipped: %s", exc)
    return out


def apply(query: str, fixes: Dict[str, str]) -> str:
    if not fixes:
        return query
    return " ".join(fixes.get(w, w) for w in query.split())
