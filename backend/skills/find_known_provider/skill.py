"""find_known_provider — search the user's OWN data (contacts +
Paperless + past calendar events) for a service provider category
BEFORE reaching out to Overpass.

The user's dentist that they invoice yearly should beat a random
Overpass match. Same defence-in-depth pattern as find_recipient_address
on the Compose side."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional


# Category → tuple of keywords to match against contact names / relation
# / Paperless correspondent / calendar event titles. Bilingual.
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "dentist":       ("zahnarzt", "zahnärztin", "dentist", "dental", "zahnmedizin", "kfo", "kieferorthop"),
    "doctor":        ("arzt", "ärztin", "praxis", "doctor", "hausarzt", "facharzt", "med."),
    "pharmacy":      ("apotheke", "pharmacy", "pharmacie", "apotheker"),
    "hospital":      ("krankenhaus", "klinik", "hospital", "klinikum"),
    "veterinary":    ("tierarzt", "tierärztin", "vet", "veterinär"),
    "optician":      ("optiker", "optician", "fielmann", "apollo optik"),
    "hairdresser":   ("friseur", "frisör", "hairdresser", "barber", "haarstudio"),
    "garage":        ("werkstatt", "kfz", "autoreparatur", "garage", "mechaniker"),
    "lawyer":        ("anwalt", "anwältin", "rechtsanwalt", "kanzlei", "lawyer"),
    "tax_advisor":   ("steuerberater", "steuerberaterin", "tax advisor", "stb"),
    "accountant":    ("buchhalter", "accountant", "wp ", "wirtschaftsprüfer"),
}


def _normalize_category(category: str) -> str:
    """Map a user-supplied keyword to the canonical category id."""
    cat = (category or "").lower().strip()
    if cat in _CATEGORY_KEYWORDS:
        return cat
    for canonical, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in cat for kw in keywords):
            return canonical
    return cat  # unknown — try the user's word as-is


def _haystack_match(haystack: str, keywords: tuple[str, ...]) -> Optional[str]:
    """Return the FIRST matching keyword found in haystack (lowercase
    substring match), or None. The matched word becomes evidence for
    why we surfaced this candidate."""
    if not haystack:
        return None
    h = haystack.lower()
    for kw in keywords:
        if kw in h:
            return kw
    return None


async def execute(
    ctx,
    category: str,
) -> dict[str, Any]:
    cat_canonical = _normalize_category(category)
    keywords = _CATEGORY_KEYWORDS.get(cat_canonical) or (cat_canonical,)

    candidates: list[dict[str, Any]] = []
    source_counts = {"contacts": 0, "paperless": 0, "calendar": 0}

    # ── 1. Contacts ───────────────────────────────────────────────
    try:
        from backend import contacts as C
        all_rows = C.search("", kind="business", status="active", limit=500)
        # Also include person-kind contacts whose relation/display_name
        # matches (e.g. "Dr. Schmidt" relation="Zahnärztin").
        all_rows += C.search("", kind="person", status="active", limit=500)
        for row in all_rows:
            full = C.get(row["id"])
            if not full: continue
            name = full.get("display_name") or ""
            relation = full.get("relation") or ""
            aliases = " ".join(full.get("aliases") or [])
            haystack = " ".join([name, relation, aliases])
            matched = _haystack_match(haystack, keywords)
            if not matched:
                continue
            addrs = full.get("addresses") or []
            addr = addrs[0] if addrs else None
            channels = full.get("channels") or []
            phone = next((c["value"] for c in channels if c["kind"] == "phone"), None)
            email = next((c["value"] for c in channels if c["kind"] == "email"), None)
            candidates.append({
                "source":      "contact",
                "contact_id":  full["id"],
                "name":        name,
                "address":     (f"{addr.get('line1','')}, {addr.get('postcode','')} {addr.get('city','')}".strip(", ")
                                  if addr else ""),
                "phone":       phone,
                "email":       email,
                "evidence":    f"contact relation/name matches «{matched}»",
            })
            source_counts["contacts"] += 1
    except Exception:
        pass

    # ── 2. Paperless past correspondents ─────────────────────────
    # Hit the connector; if Paperless isn't configured we silently skip.
    try:
        from backend.connectors import invoke as _invoke
        # Use a broad search query — first keyword usually picks up the
        # right correspondents (e.g. "zahnarzt" matches "Zahnarzt Schmidt
        # GmbH" as correspondent).
        seen_titles: set[str] = set()
        for kw in keywords[:3]:  # cap LLM-side: top 3 keywords per category
            r = await _invoke("paperless", {
                "op": "search", "query": kw, "limit": 8,
            })
            for d in (r or {}).get("documents") or []:
                corr = (d.get("correspondent") or "").strip()
                if not corr or corr in seen_titles: continue
                # Avoid re-adding contacts already found above by name.
                if any(c["name"].lower() == corr.lower() for c in candidates):
                    continue
                seen_titles.add(corr)
                candidates.append({
                    "source":           "paperless",
                    "name":             corr,
                    "evidence":         f"Paperless correspondent on «{d.get('title','(untitled)')}» ({(d.get('created') or '')[:10]})",
                    "last_seen":        (d.get("created") or "")[:10],
                    "paperless_doc_id": d.get("id"),
                })
                source_counts["paperless"] += 1
                if source_counts["paperless"] >= 5: break
            if source_counts["paperless"] >= 5: break
    except Exception:
        pass

    # ── 3. Past calendar events with locations ───────────────────
    try:
        from backend.database import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, title, starts_at, location, location_lat, location_lon "
                "FROM events "
                "WHERE location IS NOT NULL AND TRIM(location) <> '' "
                "ORDER BY starts_at DESC LIMIT 80",
            ).fetchall()
        for r in rows:
            matched = _haystack_match(r["title"] or "", keywords)
            if not matched: continue
            # Skip if already covered by a contact above (rough heuristic).
            if any(c.get("address") and r["location"]
                    and c["address"].lower() in (r["location"] or "").lower()
                    for c in candidates):
                continue
            candidates.append({
                "source":     "calendar",
                "name":       r["title"],
                "address":    r["location"],
                "evidence":   f"past appointment on {(r['starts_at'] or '')[:10]}",
                "last_seen":  (r["starts_at"] or "")[:10],
                "event_id":   r["id"],
            })
            source_counts["calendar"] += 1
            if source_counts["calendar"] >= 3: break
    except Exception:
        pass

    # DEFINITIVE marker — every branch needs to tell the LLM that this
    # response IS the authoritative answer, so it doesn't go off
    # fishing via run_sql to "double-check." The audit caught a turn
    # where the LLM ignored a clear "no contact" answer and ran 7 SQL
    # queries against hallucinated column names before agreeing with
    # the original result.
    DEFINITIVE = (
        "\n\nDEFINITIVE: this is the authoritative answer across "
        "contacts + Paperless + calendar. Do NOT run additional run_sql "
        "queries to re-verify — the search above already covered those "
        "tables. Trust this result and proceed (or ask the user)."
    )
    contacts_n = source_counts["contacts"]

    if not candidates:
        hint = (
            f"No known {cat_canonical} found in contacts/Paperless/calendar. "
            f"NEXT STEP: call find_provider_nearby(poi='{cat_canonical}', near='<user's city>') "
            "to search OpenStreetMap. After the user picks, save them with add_contact "
            "for next time."
        ) + DEFINITIVE
    elif len(candidates) == 1:
        c = candidates[0]
        hint = (
            f"Found ONE known {cat_canonical}: {c['name']} "
            f"({c['evidence']}). PROCEED — use this as the provider. "
            "If creating an appointment, pass location=<their address> to add_calendar_event."
        ) + DEFINITIVE
    else:
        lines = [f"  • {c['name']} (source={c['source']}, {c['evidence']})"
                  for c in candidates[:5]]
        # When all candidates come from calendar/Paperless (no actual
        # contact row), the LLM tends to re-verify "is there REALLY no
        # contact?" via SQL. Pre-empt that explicitly.
        contacts_caveat = (
            "\n\nNOTE: zero matches came from the contacts hub — only "
            "calendar / Paperless mentions. This means the user has NOT "
            "saved this provider as a contact. That fact is already "
            "established here; do NOT re-check via run_sql."
            if contacts_n == 0 else ""
        )
        hint = (
            f"Multiple known {cat_canonical}(s):\n" + "\n".join(lines) +
            contacts_caveat +
            f"\n\nNEXT STEP: ask the user which one — quote their name + last-seen "
            "date so they can pick quickly."
        ) + DEFINITIVE

    return {
        "_llm_hint":     hint,
        "ok":            True,
        "category":      cat_canonical,
        "candidates":    candidates,
        "source_counts": source_counts,
        "found_any":     len(candidates) > 0,
    }
