"""Phrase-replay cache for the agent loop.

After the 3rd identical user phrase, ``/api/ask`` short-circuits the LLM
entirely and replays the cached SELECT + frozen response text. Saves
~2-3s on repeat questions like "wie viele Termine habe ich heute?".

The cache is the SQLite table ``saved_queries`` (schema is in
``backend/database.py``). Keyed on a normalised lower-cased phrase
prefixed with the user's language so a German-cached answer doesn't
get served to an English speaker.

Critical gates (these were the bug-fixes that came out of the
fabricated-success investigation; do NOT remove them):

1. ``_is_safe_to_cache`` rejects anything that isn't a pure SELECT —
   re-running a cached INSERT/UPDATE/DELETE on replay would silently
   double-write.
2. The caller (the loop) must ALSO gate ``cache_save`` on
   ``_mutation_skill_invoked`` — even a SELECT can be paired with a
   ``use_skill(name="update_calendar_event", ...)`` call in the same
   turn, and replaying the SELECT + frozen "Hab den Termin verschoben"
   without re-running the skill is the original bug.

Yorik's ``agent.loop.ask`` enforces both. Extracted from
``vanna_agent.py`` verbatim — same SQL, same semantics, just lives in
its own module now.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from ..database import DEFAULT_DB_PATH, conn_ctx
import os

logger = logging.getLogger("yorik.agent.cache")

DB_PATH = os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)

# A phrase must be seen this many times before cache replay kicks in.
# (use_count > CACHE_THRESHOLD, i.e. 3+ observations.) Lower than 2
# means transient typos get replayed; higher than 2 wastes warm-up
# opportunities.
CACHE_THRESHOLD = 2

_PUNCT_TRAIL_RE = re.compile(r"[\s.?!,;:]+$")
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Phrase normalisation
# ---------------------------------------------------------------------------


def normalize_phrase(msg: str) -> str:
    """Lower-case, strip trailing punctuation, collapse whitespace.

    Pure function. Same normalisation Vanna's cache used; preserved
    bit-for-bit so existing cache rows stay valid across the cutover.
    """
    s = (msg or "").lower().strip()
    s = _PUNCT_TRAIL_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s)
    return s


def make_cache_key(user_language: str, message: str) -> str:
    """Build the saved_queries.trigger_phrase value used as the cache key.

    Format: ``"{lang}::{normalised_message}"``. Language namespacing is
    essential — a German-cached "Hab den Termin verschoben" must not
    replay for an English speaker who'd see frozen German text.
    """
    lang = (user_language or "en").lower().strip() or "en"
    return f"{lang}::{normalize_phrase(message)}"


# ---------------------------------------------------------------------------
# Safety gate
# ---------------------------------------------------------------------------


def is_safe_to_cache(sql: str) -> bool:
    """Pure SELECT only. Refuses if the SQL contains any write keyword
    as a whole word (defensive — catches subqueries that hide an
    UPDATE)."""
    if not sql:
        return False
    head = sql.strip().lstrip("(").lstrip().lower()
    if not head.startswith("select"):
        return False
    if re.search(r"\b(insert|update|delete|replace|create|drop|alter|attach)\b", sql, re.IGNORECASE):
        return False
    return True


# ---------------------------------------------------------------------------
# Lookup / save
# ---------------------------------------------------------------------------


def cache_lookup(phrase_norm: str) -> Optional[Dict[str, Any]]:
    """Return the cached row if its use_count > threshold AND safe to cache.

    Always increments use_count + updates last_used so cache "warmth"
    tracks usage even when we're still below the threshold (warming
    counter is what unlocks the next call).
    """
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, sql_query, view_command, response_text, use_count "
            "FROM saved_queries WHERE trigger_phrase = ?",
            (phrase_norm,),
        ).fetchone()
        if not row:
            return None
        # Bump regardless — counts every observation of the phrase, not
        # just hits.
        conn.execute(
            "UPDATE saved_queries SET use_count = use_count + 1, "
            "last_used = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        if row["use_count"] + 1 <= CACHE_THRESHOLD + 1:
            # Not warm enough yet (post-increment we need > threshold to hit).
            return None
        if not is_safe_to_cache(row["sql_query"]):
            return None
        return {
            "sql_query": row["sql_query"],
            "view_command": row["view_command"],
            "response_text": row["response_text"],
            "use_count": row["use_count"] + 1,
        }


def cache_save(
    phrase_norm: str,
    sql: str,
    ui_actions: List[Dict[str, Any]],
    response_text: str,
) -> None:
    """Insert or update the cache row for this phrase. No-op if SQL isn't
    cacheable. The caller MUST gate this on the mutation-skill ContextVar
    before calling — see the module docstring for why.
    """
    if not phrase_norm or not is_safe_to_cache(sql):
        return
    view_cmd_json = json.dumps(ui_actions or []) if ui_actions else None
    with conn_ctx(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT id FROM saved_queries WHERE trigger_phrase = ?",
            (phrase_norm,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE saved_queries SET sql_query = ?, view_command = ?, "
                "response_text = ?, last_used = datetime('now') WHERE id = ?",
                (sql, view_cmd_json, response_text, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO saved_queries (trigger_phrase, sql_query, "
                "view_command, response_text, use_count, last_used) "
                "VALUES (?, ?, ?, ?, 1, datetime('now'))",
                (phrase_norm, sql, view_cmd_json, response_text),
            )


def execute_cached_sql(sql: str) -> List[Dict[str, Any]]:
    """Run a cached SELECT and return up to 50 rows. Used to materialise
    fresh data when the cache hits (the response_text is frozen but the
    rows reflect today's DB state).
    """
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows][:50]


__all__ = [
    "CACHE_THRESHOLD",
    "DB_PATH",
    "normalize_phrase",
    "make_cache_key",
    "is_safe_to_cache",
    "cache_lookup",
    "cache_save",
    "execute_cached_sql",
]
