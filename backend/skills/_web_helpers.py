"""Shared helpers for web_lookup + web_fetch skills.

Two responsibilities:

  1. **PII stripping**: scrub the user's own name, address tokens, and
     contact display-names out of outbound search queries. Bare
     keywords (e.g. "Steuerberater Hannover") are fine to send; embedded
     personal context (e.g. "Steuerberater für Anna Beispielmensch")
     isn't.

  2. **Audit log**: insert a row into web_visits for every search or
     fetch so the user can review what Yorik did via Settings → Privacy.

Kept out of the connectors themselves so the LLM-facing layer owns the
"what's safe to send" + "what gets remembered" decisions.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger("yorik.skills.web_helpers")


def _pii_phrases(user_id: str) -> list[str]:
    """Build the redaction list. Two CATEGORIES, each kept as PHRASES
    (multi-word) not single tokens — so generic words like 'Hannover',
    'für', 'Müller' don't get scrubbed out of legitimate queries:

      1. The user's own full name (first + last) AND street address —
         these uniquely identify them and shouldn't ride along on a
         search query for ANY reason. Single-word forms are dropped
         deliberately ('Anna' alone isn't PII — could be a band, a
         town, a question about anything Anna-related).

      2. Contact display-names of 2+ words (e.g. 'Anna Beispielmensch',
         'Hausverwaltung Müller GmbH'). Single-word contact names
         ('Marco', 'Müller') are NOT redacted because they almost
         always overlap with legitimate query terms — a thousand-
         contact address book would otherwise nuke nearly any query.
    """
    phrases: set[str] = set()
    from backend.database import conn_ctx, DEFAULT_DB_PATH

    with conn_ctx(DEFAULT_DB_PATH) as conn:
        u = conn.execute(
            "SELECT first_name, last_name, name, address_street "
            "FROM user_profiles WHERE id = ?",
            (user_id,),
        ).fetchone()
        if u:
            # User's full name(s) — phrase only.
            full = " ".join(filter(None, [u["first_name"], u["last_name"]])).strip()
            if full and " " in full:
                phrases.add(full)
            if u["name"] and " " in u["name"] and u["name"] != full:
                phrases.add(u["name"].strip())
            # Street with house number is uniquely identifying.
            street = (u["address_street"] or "").strip()
            if street and len(street.split()) >= 2:
                phrases.add(street)

        # Contact display names — only multi-word phrases.
        rows = conn.execute(
            "SELECT display_name FROM contacts WHERE status IN ('active','pending')",
        ).fetchall()
        for r in rows:
            name = (r["display_name"] or "").strip()
            if name and len(name.split()) >= 2:
                phrases.add(name)

    # Longest first so multi-word phrases match before any shorter
    # substring (defensive — re.sub doesn't actually overlap, but order
    # makes debug output sensible).
    return sorted(phrases, key=len, reverse=True)


# Back-compat alias for callers that imported the old name. Same
# return shape (list of strings); the meaning shifted from "any word"
# to "phrase only" which is the safer + more useful behaviour.
_pii_tokens = _pii_phrases


def redact_pii(query: str, user_id: str) -> tuple[str, list[str]]:
    """Returns (redacted_query, redacted_terms). Phrase-based: only
    multi-word PII matches are removed, so generic words ('Hannover',
    'Müller') survive as legitimate query terms. See _pii_phrases for
    the policy."""
    phrases = _pii_phrases(user_id)
    if not phrases:
        return query, []
    out = query
    removed: list[str] = []
    for phrase in phrases:
        # Word-boundary phrase match, case-insensitive. Allow internal
        # whitespace flexibility — "Anna  Beispielmensch" still matches.
        pattern = r"\b" + r"\s+".join(re.escape(p) for p in phrase.split()) + r"\b"
        pat = re.compile(pattern, re.IGNORECASE)
        if pat.search(out):
            out = pat.sub("", out)
            removed.append(phrase)
    out = re.sub(r"\s{2,}", " ", out).strip(" -,;.")
    return (out, removed)


def audit_log(
    user_id: Optional[int],
    *,
    action: str,
    query: Optional[str] = None,
    url: Optional[str] = None,
    provider: Optional[str] = None,
    ok: bool = True,
    status: Optional[int] = None,
    bytes_: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """Best-effort audit log write. Never raises."""
    if user_id is None:
        return
    try:
        from backend.database import conn_ctx, DEFAULT_DB_PATH
        with conn_ctx(DEFAULT_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO web_visits "
                "(user_id, action, query, url, provider, ok, status, bytes, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, action, query, url, provider,
                 1 if ok else 0, status, bytes_, (error or None)),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.exception("audit_log failed: %s", exc)
