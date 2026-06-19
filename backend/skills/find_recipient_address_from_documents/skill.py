"""find_recipient_address_from_documents — mine postal addresses out of
past Paperless documents for a given contact.

Sits between compose_check_recipient (which says "no address") and
either compose_draft (when the user confirms a Paperless hit) or a
direct ask to the user (when nothing is found). Caches results in
contact_address_suggestions with source_kind='paperless' so repeat calls
for the same contact are instant.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

log = logging.getLogger("yorik.skills.find_recipient_address_from_documents")


# Cache TTL — addresses on past letters rarely change, so 30 days is
# generous without becoming truly stale. Force-refresh via use_cache=False.
_CACHE_TTL_DAYS = 30

# How much OCR text to feed the LLM per document. Letterheads + senders
# almost always sit in the first and last 2 KB; the middle is body
# content that wastes tokens.
_HEAD_CHARS = 2000
_TAIL_CHARS = 2000

# Safety cap on documents per call. Paperless OCR can be slow and the
# LLM call is the dominant cost; 5 strikes the right balance.
_MAX_DOCS_HARD = 10


def _paperless_settings() -> Optional[dict[str, str]]:
    """Lazy peek at credentials so we can fail fast with a clear hint
    when Paperless isn't configured."""
    try:
        from backend.connectors.paperless import _settings
        s = _settings()
        if not s.get("api_key"):
            return None
        return s
    except Exception:  # noqa: BLE001
        return None


def _fetch_doc_full(doc_id: int, settings: dict[str, str]) -> Optional[dict[str, Any]]:
    """Fetch a single Paperless document including its `content` (OCR
    text). The shared connector returns a slim view that strips content;
    we need the raw payload here."""
    try:
        r = requests.get(
            f"{settings['base_url']}/api/documents/{int(doc_id)}/",
            headers={
                "Authorization": f"Token {settings['api_key']}",
                "Accept":        "application/json",
            },
            timeout=8,
        )
        r.raise_for_status()
        return r.json() or None
    except requests.RequestException as exc:
        log.warning("paperless fetch for doc %s failed: %s", doc_id, exc)
        return None


def _find_relevant_docs(contact: dict[str, Any], max_docs: int) -> list[dict[str, Any]]:
    """Find Paperless docs likely to mention this contact's postal
    address. Two passes:
      1. Documents where the contact is the Paperless `correspondent`
         (highest signal — these are letters to/from this person).
      2. Full-text search for the contact's display_name (catches docs
         where the name appears in the body but no correspondent set).
    Dedupes by doc id, ordered correspondent-first."""
    from backend.connectors.paperless import paperless as pl_call

    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    # Pass 1 — correspondent match.
    try:
        r = pl_call(op="by_correspondent",
                    correspondent=contact["display_name"], limit=max_docs)
        for d in r.get("documents") or []:
            did = d.get("id")
            if did and did not in seen:
                seen.add(did)
                out.append(d)
    except Exception as exc:  # noqa: BLE001
        log.debug("by_correspondent failed for %s: %s", contact.get("display_name"), exc)

    # Pass 2 — full-text search (don't double up on correspondent hits).
    if len(out) < max_docs:
        try:
            r = pl_call(op="search",
                        query=contact["display_name"],
                        limit=max_docs - len(out))
            for d in r.get("documents") or []:
                did = d.get("id")
                if did and did not in seen:
                    seen.add(did)
                    out.append(d)
        except Exception as exc:  # noqa: BLE001
            log.debug("search failed for %s: %s", contact.get("display_name"), exc)

    return out[:max_docs]


def _read_paperless_cache(contact_id: int) -> list[dict[str, Any]]:
    """Return cached paperless candidates if any exist AND aren't older
    than _CACHE_TTL_DAYS. Empty list otherwise."""
    from backend.database import get_conn

    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=_CACHE_TTL_DAYS)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT line1, line2, postcode, city, region, country, "
            "       confidence, excerpt, source_kind, source_ref, scraped_at "
            "FROM contact_address_suggestions "
            "WHERE contact_id = ? "
            "  AND source_kind = 'paperless' "
            "  AND scraped_at >= ? "
            "ORDER BY confidence DESC NULLS LAST, scraped_at DESC",
            (contact_id, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def _write_paperless_cache(contact_id: int, candidates: list[dict[str, Any]]) -> None:
    """Replace this contact's paperless cache entries with the new set.
    Leaves whatsapp/email entries alone so the two scraper paths don't
    clobber each other."""
    from backend.database import get_conn

    with get_conn() as conn:
        conn.execute(
            "DELETE FROM contact_address_suggestions "
            "WHERE contact_id = ? AND source_kind = 'paperless'",
            (contact_id,),
        )
        for c in candidates:
            conn.execute(
                "INSERT INTO contact_address_suggestions "
                "(contact_id, source_kind, source_ref, line1, line2, postcode, "
                " city, region, country, confidence, excerpt) "
                "VALUES (?, 'paperless', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    contact_id,
                    str(c.get("source_doc_id") or ""),
                    c.get("line1"), c.get("line2"),
                    c.get("postcode"), c.get("city"),
                    c.get("region"), c.get("country"),
                    c.get("confidence"), c.get("excerpt"),
                ),
            )
        conn.commit()


def _decorate_cached(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cached rows lack the source_doc_title since we only saved the
    doc id. Add a best-effort title via a Paperless lookup. Failures
    return the raw row — the doc id is still useful to the user."""
    settings = _paperless_settings()
    out: list[dict[str, Any]] = []
    for r in rows:
        doc_id = None
        try:
            doc_id = int(r.get("source_ref") or 0) or None
        except (TypeError, ValueError):
            pass
        title = None
        date = None
        if settings and doc_id:
            full = _fetch_doc_full(doc_id, settings)
            if full:
                title = full.get("title")
                date = full.get("created_date") or full.get("created")
        out.append({
            "line1":            r.get("line1"),
            "line2":            r.get("line2"),
            "postcode":         r.get("postcode"),
            "city":             r.get("city"),
            "region":           r.get("region"),
            "country":          r.get("country"),
            "confidence":       r.get("confidence"),
            "source_doc_id":    doc_id,
            "source_doc_title": title or "(cached)",
            "source_doc_date":  date,
            "excerpt":          r.get("excerpt"),
        })
    return out


def _emit_needs_input(
    contact_id: int,
    contact_name: str,
    candidates: list[dict[str, Any]],
    *,
    template_id: Optional[str],
) -> None:
    """Push a needs_input ui_action so ComposeAgentChat renders an inline
    form card with the Paperless-mined candidates as quick-fill chips.
    Empty candidates → blank form (user types from scratch). Either way
    the user has a fast click path that doesn't depend on the LLM."""
    from backend.ui_tools import _append

    suggestions = []
    for c in candidates[:3]:
        src = c.get("source_doc_title") or f"doc #{c.get('source_doc_id')}"
        date = (c.get("source_doc_date") or "")[:10]
        label_bits = [f"Aus „{src}\""]
        if date:
            label_bits.append(f"({date})")
        suggestions.append({
            "label":  " ".join(label_bits),
            "values": {
                "line1":    c.get("line1") or "",
                "postcode": c.get("postcode") or "",
                "city":     c.get("city") or "",
            },
            "source_doc_id":    c.get("source_doc_id"),
            "source_doc_title": c.get("source_doc_title"),
            "confidence":       c.get("confidence"),
        })

    fields = [
        {"key": "line1",    "label": "Straße + Hausnr.", "value": "", "required": True},
        {"key": "postcode", "label": "PLZ",              "value": "", "required": False,
         "pattern": r"^\d{4,5}$"},
        {"key": "city",     "label": "Ort",              "value": "", "required": True},
    ]

    context = (
        f"Yorik hat {len(candidates)} mögliche Adresse(n) für {contact_name} "
        "in alten Dokumenten gefunden — wähle eine oder gib eine andere an:"
        if candidates else
        f"Für {contact_name} gibt es noch keine gespeicherte Postanschrift "
        "und auch keine in deinen Dokumenten. Bitte trag sie kurz ein:"
    )

    payload: dict[str, Any] = {
        "type":        "needs_input",
        "source_skill": "find_recipient_address_from_documents",
        "title":       f"Postanschrift von {contact_name}",
        "context":     context,
        "fields":      fields,
        "suggestions": suggestions,
        "save_to_contact": {
            "contact_id":      contact_id,
            "kind":            "home",
            "default_checked": True,
            "label":           f"Adresse bei {contact_name} speichern (für nächstes Mal)",
        },
        # After this form is submitted (the user picked/typed an
        # address), the LLM should still verify template-specific args
        # before drafting. Address ≠ template completeness.
        "next_playbook_step": "compose_check_template_args",
        "resume_skill": "compose_check_template_args",
        "resume_args":  {"contact_id": contact_id},
    }
    if template_id:
        payload["resume_args"]["template_id"] = template_id
    _append(payload)


def _format_hint(contact_name: str, candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return (
            f"No postal addresses found in Paperless for {contact_name}. "
            "NEXT STEP: ask the user ONE short question in their language: "
            f"'Welche Postanschrift hat {contact_name}? (Straße + Hausnr., PLZ, Ort)'. "
            "When they answer, call add_contact_address(contact_id=…, kind='home', …) "
            "to save it, then compose_draft."
        )
    top = candidates[0]
    pretty = f"{top.get('line1') or ''}".strip()
    plz_city = " ".join(filter(None, [(top.get('postcode') or '').strip(),
                                       (top.get('city') or '').strip()]))
    if plz_city:
        pretty = f"{pretty}, {plz_city}" if pretty else plz_city
    src = top.get("source_doc_title") or f"doc #{top.get('source_doc_id')}"
    n = len(candidates)
    return (
        f"Found {n} candidate address(es) for {contact_name} in Paperless. "
        f"Top match: {pretty} (source: '{src}'). "
        "NEXT STEP: present the top match to the user as ONE short question — "
        f"'Soll ich an {pretty} schicken? (Aus „{src}\")'. "
        "If they confirm: call add_contact_address(contact_id=…, kind='home', "
        "line1=…, postcode=…, city=…) to save it for next time, THEN call "
        "compose_draft. If they say no or want a different address: ask them "
        "for the correct one directly. Never silently pick — confirmation always."
    )


async def execute(
    ctx,
    contact_id: int,
    max_docs: int = 5,
    use_cache: bool = True,
) -> dict[str, Any]:
    from backend import contacts as C

    try:
        cid = int(contact_id)
    except (TypeError, ValueError):
        return {"_llm_hint": f"contact_id must be int; got {contact_id!r}.",
                "candidates": [], "scanned_count": 0, "cached": False}

    contact = C.get(cid)
    if not contact:
        return {"_llm_hint": f"contact_id={cid} not found.",
                "candidates": [], "scanned_count": 0, "cached": False}

    name = contact["display_name"]

    settings = _paperless_settings()
    if not settings:
        return {
            "_llm_hint": (
                "Paperless isn't configured on this Yorik. Skip this step and "
                f"ask the user for {name}'s postal address directly."
            ),
            "candidates":    [],
            "scanned_count": 0,
            "cached":        False,
            "contact_id":    cid,
            "contact_name":  name,
        }

    # ── Cache path ──
    if use_cache:
        cached = _read_paperless_cache(cid)
        if cached:
            decorated = _decorate_cached(cached)
            _emit_needs_input(cid, name, decorated, template_id=None)
            return {
                "_llm_hint":    _format_hint(name, decorated),
                "candidates":   decorated,
                "scanned_count": 0,
                "cached":       True,
                "contact_id":   cid,
                "contact_name": name,
            }

    # ── Fresh scan ──
    try:
        max_docs_clamped = max(1, min(int(max_docs), _MAX_DOCS_HARD))
    except (TypeError, ValueError):
        max_docs_clamped = 5

    doc_metas = await asyncio.to_thread(_find_relevant_docs, contact, max_docs_clamped)

    if not doc_metas:
        return {
            "_llm_hint":    _format_hint(name, []),
            "candidates":   [],
            "scanned_count": 0,
            "cached":       False,
            "contact_id":   cid,
            "contact_name": name,
        }

    # Fetch OCR content. Run sequentially (Paperless can be slow under load
    # and asyncio.gather of 5 blocking requests with to_thread is fine but
    # gives us less observability). Build passages keeping head + tail.
    passages: list[dict[str, Any]] = []
    for meta in doc_metas:
        full = await asyncio.to_thread(_fetch_doc_full, meta["id"], settings)
        if not full:
            continue
        content = (full.get("content") or "").strip()
        if not content:
            continue
        if len(content) > _HEAD_CHARS + _TAIL_CHARS + 100:
            text = content[:_HEAD_CHARS] + "\n\n…\n\n" + content[-_TAIL_CHARS:]
        else:
            text = content
        passages.append({
            "source_kind": "paperless",
            "source_ref":  str(meta["id"]),
            "text":        text,
            "_meta":       {
                "doc_id":    meta["id"],
                "doc_title": full.get("title") or meta.get("title") or "(untitled)",
                "doc_date":  full.get("created_date") or full.get("created") or meta.get("created"),
            },
        })

    if not passages:
        return {
            "_llm_hint": (
                f"Found {len(doc_metas)} Paperless doc(s) for {name} but none "
                "had OCR text yet — Paperless may still be processing them. "
                "Ask the user for the address directly."
            ),
            "candidates":   [],
            "scanned_count": 0,
            "cached":       False,
            "contact_id":   cid,
            "contact_name": name,
        }

    # Reuse the existing scraper's LLM extractor — same prompt that already
    # produces good results on WhatsApp/email passages. Passages still need
    # the source_index back-reference for attribution.
    from backend.contact_address_scraper import call_llm_extract, _coerce_float
    llm_passages = [{"source_kind": p["source_kind"], "source_ref": p["source_ref"],
                      "text": p["text"]} for p in passages]
    raw_hits = await asyncio.to_thread(call_llm_extract, llm_passages)

    candidates: list[dict[str, Any]] = []
    for h in raw_hits:
        si = h.get("source_index")
        try:
            si_int = int(si) if si is not None else None
        except (TypeError, ValueError):
            si_int = None
        if si_int is None or not (0 <= si_int < len(passages)):
            continue
        meta = passages[si_int]["_meta"]
        line1 = (h.get("line1") or "").strip() or None
        city = (h.get("city") or "").strip() or None
        if not (line1 or city):
            continue
        candidates.append({
            "line1":            line1,
            "line2":            (h.get("line2") or "").strip() or None,
            "postcode":         (h.get("postcode") or "").strip() or None,
            "city":             city,
            "region":           (h.get("region") or "").strip() or None,
            "country":          ((h.get("country") or "").strip()[:2] or None),
            "confidence":       _coerce_float(h.get("confidence")),
            "source_doc_id":    meta["doc_id"],
            "source_doc_title": meta["doc_title"],
            "source_doc_date":  meta["doc_date"],
            "excerpt":          (h.get("excerpt") or "").strip()[:200] or None,
        })

    # Sort by confidence DESC (Nones last) so the top of the list is the
    # strongest match.
    candidates.sort(key=lambda c: (c.get("confidence") is None,
                                     -(c.get("confidence") or 0)))

    _write_paperless_cache(cid, candidates)

    _emit_needs_input(cid, name, candidates, template_id=None)

    return {
        "_llm_hint":    _format_hint(name, candidates),
        "candidates":   candidates,
        "scanned_count": len(passages),
        "cached":       False,
        "contact_id":   cid,
        "contact_name": name,
    }
