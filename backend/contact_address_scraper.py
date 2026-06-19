"""
On-demand address scraper.

Fed the user's WhatsApp + email history with a specific contact, asks the
local LLM (qwen) to extract any postal addresses mentioned in the text.
The result is cached in `contact_address_suggestions` so re-opens of the
"add address" form are instant.

User-triggered only — runs when the user clicks "Search messages for
an address" inside the address editor. We don't auto-scan all 535 pending
contacts because (a) most will never get a letter and (b) the qwen 27b
isn't free.

Output shape (per candidate):
  {
    line1, line2, postcode, city, region, country,
    confidence (0-1), source_kind ('email'|'whatsapp'),
    source_ref (msg id, for "where did Yorik find this?"),
    excerpt (the original passage)
  }
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from .database import get_conn
from . import contacts as _contacts

log = logging.getLogger(__name__)


# Max passages we feed the LLM in one shot. Each ~500 chars → ~50 tokens
# input, plus our prompt scaffolding. Caps total request at ~3k input
# tokens which qwen handles comfortably in <5s.
_MAX_PASSAGES = 30
_PASSAGE_MAX_CHARS = 800


def gather_passages(contact_id: int, *, owner_user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Pull the candidate text snippets we'll feed the LLM. Mixes
    WhatsApp + email so the LLM sees both channels' conventions."""
    c = _contacts.get(contact_id, include_children=True)
    if not c:
        return []

    out: List[Dict[str, Any]] = []
    wa_numbers = [ch["value"] for ch in c.get("channels", []) if ch["kind"] == "whatsapp"]
    emails     = [ch["value"] for ch in c.get("channels", []) if ch["kind"] == "email"]

    with get_conn() as conn:
        # WhatsApp messages — both directions; addresses get shared by
        # either party ("our place is at...", "send to...").
        if wa_numbers:
            jids = [f"{n}@s.whatsapp.net" for n in wa_numbers]
            placeholders = ",".join("?" * len(jids))
            rows = conn.execute(
                f"""
                SELECT msg_id, text, timestamp
                FROM wa_messages
                WHERE chat_jid IN ({placeholders})
                  AND text IS NOT NULL AND TRIM(text) <> ''
                  AND LENGTH(text) > 15   -- skip "ok", "yes", etc.
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                [*jids, _MAX_PASSAGES],
            ).fetchall()
            for r in rows:
                out.append({
                    "source_kind": "whatsapp",
                    "source_ref":  r["msg_id"],
                    "text":        (r["text"] or "")[:_PASSAGE_MAX_CHARS],
                })

        # Emails — body_text is usually richer than WA. Pull both
        # incoming (from this address) AND outgoing (to this address)
        # because either may have signed off with an address block.
        if emails:
            email_clauses = " OR ".join(["LOWER(from_email) = ?"] * len(emails)
                                         + ["to_addrs LIKE ?"] * len(emails))
            params: List[Any] = []
            for e in emails:
                params.append(e.lower())
            for e in emails:
                params.append(f'%"{e.lower()}"%')
            if owner_user_id is not None:
                where_owner = "AND owner_user_id = ?"
                params.append(owner_user_id)
            else:
                where_owner = ""
            params.append(_MAX_PASSAGES)
            rows = conn.execute(
                f"""
                SELECT id, body_text, subject, date_received
                FROM email_messages
                WHERE ({email_clauses}) {where_owner}
                  AND body_text IS NOT NULL AND TRIM(body_text) <> ''
                ORDER BY date_received DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            for r in rows:
                # Email bodies are long — chop early. Most signatures live
                # in the last 500 chars, so prefer the tail.
                body = r["body_text"] or ""
                if len(body) > _PASSAGE_MAX_CHARS:
                    body = body[-_PASSAGE_MAX_CHARS:]
                out.append({
                    "source_kind": "email",
                    "source_ref":  str(r["id"]),
                    "text":        body,
                })

    # Trim to the global cap. Mix interleaves WA + email so the LLM gets
    # diverse signal even when one channel dominates.
    out.sort(key=lambda x: 0)  # stable
    return out[:_MAX_PASSAGES]


_PROMPT_HEADER = """You are a structured-data extractor. The following passages are messages
between the user and one specific person. Extract any POSTAL addresses
mentioned in the text — both the person's own address and any
delivery/billing/shipping addresses they shared.

Skip company office addresses unless clearly attached to this person
as theirs. Skip URLs, email addresses, phone numbers, and casual
location mentions ("we went to Berlin") that aren't postal addresses.

For each address found, return a JSON object with these fields:
- line1     (string, street + number)
- line2     (string, apartment / floor / etc — null if none)
- postcode  (string)
- city      (string)
- region    (string, state/canton/Bundesland — null if not applicable)
- country   (string, ISO-3166-1 alpha-2, e.g. "DE" / "IT" / "US" — best guess)
- confidence (0.0-1.0, your confidence this is really a postal address belonging to this person)
- source_index (integer, the [n] of the passage you found it in)
- excerpt   (string, the short snippet of original text that contained the address)

Return ONLY a JSON array. Empty array [] if nothing found. No prose, no markdown."""


def build_prompt(passages: List[Dict[str, Any]]) -> str:
    parts = [_PROMPT_HEADER, ""]
    for i, p in enumerate(passages):
        kind = p["source_kind"]
        parts.append(f"--- Passage [{i}] (source: {kind}) ---")
        parts.append(p["text"])
        parts.append("")
    return "\n".join(parts)


def call_llm_extract(passages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run one chat completion and parse the JSON array out of the reply.
    Returns [] on parse failure (silent — caller should not crash)."""
    if not passages:
        return []
    from .agent.llm import LlmClient
    client = LlmClient(
        model=os.getenv("HOMEOS_MODEL", "qwen3.6-27b-mtp"),
        base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
    )
    prompt = build_prompt(passages)
    try:
        resp = client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("address scrape LLM call failed: %s", exc)
        return []

    content = (resp.get("content") or "").strip()
    # The model sometimes wraps the JSON in ```json … ``` fences despite
    # the prompt — strip them defensively.
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    if not content:
        return []
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        log.debug("address scrape: JSON parse failed: %s; raw=%r", exc, content[:200])
        # Fallback: try to extract the first JSON array out of mixed text.
        m = re.search(r"\[\s*(?:\{.*?\}\s*,?\s*)*\]", content, re.DOTALL)
        if not m:
            return []
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []

    if not isinstance(parsed, list):
        return []
    return [c for c in parsed if isinstance(c, dict)]


def scrape_and_cache(
    contact_id: int,
    *,
    owner_user_id: Optional[int] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Top-level entry. Returns a summary dict the API hands to the UI.

    Shape: {scraped_at: iso, candidates: [...]}.

    If `use_cache=True` (default) and we already have cached suggestions
    AND a scraped_at timestamp on the contact, returns the cache without
    calling the LLM. Pass False to force a re-scrape.
    """
    with get_conn() as conn:
        meta = conn.execute(
            "SELECT address_scraped_at FROM contacts WHERE id = ?",
            (contact_id,),
        ).fetchone()
        if not meta:
            return {"scraped_at": None, "candidates": [], "error": "contact not found"}

        if use_cache and meta["address_scraped_at"]:
            rows = conn.execute(
                "SELECT id, source_kind, source_ref, line1, line2, postcode, city, "
                "       region, country, confidence, excerpt, scraped_at "
                "FROM contact_address_suggestions "
                "WHERE contact_id = ? "
                "ORDER BY confidence DESC NULLS LAST, scraped_at DESC",
                (contact_id,),
            ).fetchall()
            return {
                "scraped_at": meta["address_scraped_at"],
                "from_cache": True,
                "candidates": [dict(r) for r in rows],
            }

    # No cache (or forced refresh) — gather + LLM.
    passages = gather_passages(contact_id, owner_user_id=owner_user_id)
    candidates_raw = call_llm_extract(passages)

    # Map LLM candidates back into our row shape. Defensive about missing
    # fields — qwen sometimes omits region/country.
    rows_to_insert: List[Dict[str, Any]] = []
    for c in candidates_raw:
        src_idx = c.get("source_index")
        try:
            src_idx_int = int(src_idx) if src_idx is not None else None
        except (TypeError, ValueError):
            src_idx_int = None
        p = passages[src_idx_int] if (src_idx_int is not None and 0 <= src_idx_int < len(passages)) else None
        rows_to_insert.append({
            "source_kind": (p or {}).get("source_kind") or "unknown",
            "source_ref":  (p or {}).get("source_ref"),
            "line1":       (c.get("line1") or "").strip() or None,
            "line2":       (c.get("line2") or "").strip() or None,
            "postcode":    (c.get("postcode") or "").strip() or None,
            "city":        (c.get("city") or "").strip() or None,
            "region":      (c.get("region") or "").strip() or None,
            "country":     ((c.get("country") or "").strip()[:2] or None),
            "confidence":  _coerce_float(c.get("confidence")),
            "excerpt":     (c.get("excerpt") or "").strip()[:400] or None,
        })
    # Filter out junk (no line1 AND no city — there's nothing useful).
    rows_to_insert = [r for r in rows_to_insert if r["line1"] or r["city"]]

    with get_conn() as conn:
        conn.execute(
            "DELETE FROM contact_address_suggestions WHERE contact_id = ?",
            (contact_id,),
        )
        for r in rows_to_insert:
            conn.execute(
                "INSERT INTO contact_address_suggestions "
                "(contact_id, source_kind, source_ref, line1, line2, postcode, city, "
                " region, country, confidence, excerpt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    contact_id, r["source_kind"], r["source_ref"], r["line1"],
                    r["line2"], r["postcode"], r["city"], r["region"],
                    r["country"], r["confidence"], r["excerpt"],
                ),
            )
        conn.execute(
            "UPDATE contacts SET address_scraped_at = datetime('now') WHERE id = ?",
            (contact_id,),
        )
        conn.commit()

    # Read back so the returned shape matches the cached path.
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, source_kind, source_ref, line1, line2, postcode, city, "
            "       region, country, confidence, excerpt, scraped_at "
            "FROM contact_address_suggestions "
            "WHERE contact_id = ? "
            "ORDER BY confidence DESC NULLS LAST, scraped_at DESC",
            (contact_id,),
        ).fetchall()
        meta = conn.execute(
            "SELECT address_scraped_at FROM contacts WHERE id = ?",
            (contact_id,),
        ).fetchone()
    return {
        "scraped_at": meta["address_scraped_at"] if meta else None,
        "from_cache": False,
        "candidates": [dict(r) for r in rows],
        "passages_scanned": len(passages),
    }


def _coerce_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f < 0:
        return 0.0
    if f > 1:
        return 1.0
    return f
