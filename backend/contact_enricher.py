"""LLM-driven contact enrichment.

Walks every contact, gathers their mentions across emails / WhatsApp /
Paperless, asks the LLM to extract structured field proposals
(birthday, address, relation, business name, etc.), and writes them to
contact_enrichment_proposals. The Edit-Contact UI then pre-fills empty
fields with the highest-confidence proposal and exposes alternatives
in a per-field dropdown.

Safety guarantees:
  - NEVER writes directly to the contacts table. All proposals are
    just suggestions; nothing changes the contact until the user
    explicitly saves the edit form (with the suggested or alternative
    value pre-filled).
  - LLM output is parsed strictly — non-JSON or unexpected shape is
    discarded silently rather than guessed at.
  - Re-running enrich_all is idempotent: each contact's pending
    proposals are wiped first, then re-inserted. Accepted/rejected
    proposals from prior runs are kept (so the user's choice isn't
    re-proposed).

Cost (rough): ~2000 prompt + 500 response tokens per contact. For
500 contacts on a local 7-9B LLM at 50 tok/s: ~5-8 hours background.
Cooperative cancel checks after every contact.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from . import workers

log = logging.getLogger("yorik.contact_enricher")

# Fields the enricher can propose values for. The Edit-Contact form
# knows about these one-to-one — adding a field here without UI
# support is harmless (proposal sits in the table, no one renders it).
ENRICHABLE_FIELDS: List[str] = [
    "relation",          # 'family' | 'friend' | 'colleague' | 'vendor' | 'customer' | ...
    "birthday",          # YYYY-MM-DD
    "kind",              # 'person' | 'business'
    "salutation_pref",   # 'du' | 'sie'
    "language_pref",     # 'de' | 'en' | ...
    "legal_name",        # 'Acme GmbH'
    "tax_id",            # 'DE123456789'
    "iban",              # 'DE89...'
    "notes",             # free-form
    "address",           # JSON {line1, line2, postcode, city, country}
]

CONTENT_SAMPLE_PER_SOURCE = 800
MAX_EMAILS_PER_CONTACT = 8
MAX_WA_MESSAGES_PER_CONTACT = 12
MAX_DOCS_PER_CONTACT = 6


def _looks_like_bare_phone(name: str) -> bool:
    """True for display_names that are just digits / +digits — most
    common after autocapture from a WhatsApp message before the user
    has set a real name. Bare phones don't match doc OCR or calendar
    titles, so the enricher needs to derive an alternate search seed
    (usually the pushName the bridge captured on the message)."""
    s = (name or "").strip().lstrip("+")
    return bool(s) and s.isdigit() and len(s) >= 6


# ─── Cooperative cancel (same pattern as autotagger) ──────────────────

_cancel_requested: bool = False


def request_cancel() -> None:
    global _cancel_requested
    _cancel_requested = True


def _reset_cancel() -> None:
    global _cancel_requested
    _cancel_requested = False


def is_cancel_requested() -> bool:
    return _cancel_requested


# ─── Data collection ──────────────────────────────────────────────────

def _collect_contact_mentions(contact_id: int) -> Dict[str, Any]:
    """Pull a digest of everywhere this contact has shown up — used as
    the LLM prompt's context. Conservative cap on each source so the
    full prompt stays under ~3000 tokens for typical contacts."""
    from . import contacts as _contacts
    from .database import get_conn

    contact = _contacts.get(contact_id, include_children=True)
    if not contact:
        return {}
    channels = contact.get("channels") or []
    email_addrs = [c["value"] for c in channels if c.get("kind") == "email"]
    wa_jids = [c["value"] for c in channels if c.get("kind") == "whatsapp"]

    out: Dict[str, Any] = {
        "contact": {
            "id":             contact["id"],
            "display_name":   contact.get("display_name") or "",
            "aliases":        contact.get("aliases") or [],
            "kind":           contact.get("kind") or "person",
            "current_fields": {
                f: contact.get(f) for f in ENRICHABLE_FIELDS
                if contact.get(f) and f != "address"
            },
        },
        "channels":  channels,
        "addresses": contact.get("addresses") or [],
        "emails":    [],
        "whatsapp":  [],
        "documents": [],
    }

    # Emails — from this contact's email addresses, latest first.
    if email_addrs:
        try:
            with get_conn() as conn:
                placeholders = ",".join("?" * len(email_addrs))
                rows = conn.execute(
                    f"SELECT subject, from_addr, snippet, body_text "
                    f"FROM email_messages "
                    f"WHERE from_addr IN ({placeholders}) "
                    f"ORDER BY received_at DESC LIMIT ?",
                    [*email_addrs, MAX_EMAILS_PER_CONTACT],
                ).fetchall()
            for r in rows:
                body = (r["body_text"] or r["snippet"] or "")[:CONTENT_SAMPLE_PER_SOURCE]
                out["emails"].append({
                    "subject":   (r["subject"] or "").strip(),
                    "from":      r["from_addr"],
                    "body":      body.strip(),
                })
        except Exception as exc:  # noqa: BLE001
            log.debug("enricher: email pull failed for %s: %s", contact_id, exc)

    # WhatsApp messages — from this contact's JIDs.
    if wa_jids:
        try:
            with get_conn() as conn:
                placeholders = ",".join("?" * len(wa_jids))
                rows = conn.execute(
                    f"SELECT chat_jid, from_me, text, transcript "
                    f"FROM wa_messages "
                    f"WHERE chat_jid IN ({placeholders}) AND text IS NOT NULL AND text != '' "
                    f"ORDER BY timestamp DESC LIMIT ?",
                    [*wa_jids, MAX_WA_MESSAGES_PER_CONTACT],
                ).fetchall()
            for r in rows:
                out["whatsapp"].append({
                    "from_me": bool(r["from_me"]),
                    "text":    (r["text"] or r["transcript"] or "")[:CONTENT_SAMPLE_PER_SOURCE],
                })
        except Exception as exc:  # noqa: BLE001
            log.debug("enricher: WA pull failed for %s: %s", contact_id, exc)

    # Build search seeds. Primary seed is display_name; for contacts
    # whose display_name is just digits (typical autocaptured-from-
    # WhatsApp-message-with-no-pushName case), we also try the most
    # recent pushName the bridge has on file for any of their WA jids.
    # That gives "4915xxxxxxx" → "Hans Becker" coverage so doc/calendar
    # FTS actually finds something. Deduped.
    raw_name = (contact.get("display_name") or "").strip()
    seeds: List[str] = []
    if raw_name and len(raw_name) >= 3:
        seeds.append(raw_name)
    if _looks_like_bare_phone(raw_name) and wa_jids:
        try:
            from .database import get_conn as _get_conn_main
            with _get_conn_main() as conn:
                ph = ",".join("?" * len(wa_jids))
                pn_rows = conn.execute(
                    f"SELECT DISTINCT push_name FROM wa_messages "
                    f"WHERE chat_jid IN ({ph}) AND push_name IS NOT NULL "
                    f"AND push_name != '' "
                    f"ORDER BY timestamp DESC LIMIT 3",
                    wa_jids,
                ).fetchall()
            for r in pn_rows:
                pn = (r["push_name"] or "").strip()
                if pn and len(pn) >= 3 and pn not in seeds:
                    seeds.append(pn)
        except Exception as exc:  # noqa: BLE001
            log.debug("enricher: pushname seed lookup failed for %s: %s", contact_id, exc)

    # Paperless docs FTS — try each seed, dedupe by doc_id, cap at
    # MAX_DOCS_PER_CONTACT total. Bare-phone contacts now benefit from
    # the pushName fallback above.
    seen_doc_ids: set[int] = set()
    if seeds:
        try:
            from . import paperless_ingest as _pp
            for seed in seeds:
                if len(out["documents"]) >= MAX_DOCS_PER_CONTACT:
                    break
                fts = _pp.search_fts(seed, k=MAX_DOCS_PER_CONTACT)
                for d in fts or []:
                    did = d.get("paperless_doc_id")
                    if did in seen_doc_ids:
                        continue
                    seen_doc_ids.add(did)
                    out["documents"].append({
                        "doc_id":  did,
                        "title":   d.get("doc_title") or "",
                        "snippet": (d.get("text") or "")[:CONTENT_SAMPLE_PER_SOURCE],
                        "matched_seed": seed,
                    })
                    if len(out["documents"]) >= MAX_DOCS_PER_CONTACT:
                        break
        except Exception as exc:  # noqa: BLE001
            log.debug("enricher: doc pull failed for %s: %s", contact_id, exc)

    # Calendar events — same multi-seed strategy. Dedupe by title+starts_at.
    if seeds:
        try:
            from .database import get_conn as _get_conn_main
            seen_evt: set[tuple[str, str]] = set()
            with _get_conn_main() as conn:
                for seed in seeds:
                    if len(out.get("calendar", [])) >= 10:
                        break
                    like = f"%{seed}%"
                    rows = conn.execute(
                        "SELECT title, starts_at, person, notes "
                        "FROM events "
                        "WHERE person = ? OR title LIKE ? OR notes LIKE ? "
                        "ORDER BY starts_at DESC LIMIT 10",
                        (seed, like, like),
                    ).fetchall()
                    for r in rows:
                        key = (r["title"] or "", r["starts_at"] or "")
                        if key in seen_evt:
                            continue
                        seen_evt.add(key)
                        snippet_parts = []
                        if r["person"]: snippet_parts.append(f"with {r['person']}")
                        if r["notes"]:  snippet_parts.append(r["notes"][:200])
                        out["calendar"] = out.get("calendar", [])
                        out["calendar"].append({
                            "title":   r["title"],
                            "when":    (r["starts_at"] or "")[:10],
                            "details": " — ".join(snippet_parts) if snippet_parts else "",
                            "matched_seed": seed,
                        })
        except Exception as exc:  # noqa: BLE001
            log.debug("enricher: calendar pull failed for %s: %s", contact_id, exc)

    # Stash the seeds we tried so the prompt + UI can show what we
    # actually searched (helps the user understand why coverage is
    # what it is — e.g. "we searched for both '+4915xxxx' and 'Hans Becker'").
    out["seeds"] = seeds
    return out


def count_contact_mentions(contact_id: int) -> Dict[str, Any]:
    """Cheap SQL-only summary of what data Yorik has on this contact —
    no LLM call, no large content fetches. Used by GET /proposals so
    the edit-contact UI can show "scanned 0 emails, 0 docs, …" even
    when no proposals exist (without re-running enrichment)."""
    from . import contacts as _contacts
    from .database import get_conn

    contact = _contacts.get(contact_id, include_children=True)
    if not contact:
        return {"emails": 0, "whatsapp": 0, "documents": 0, "calendar": 0, "seeds": []}
    channels = contact.get("channels") or []
    email_addrs = [c["value"] for c in channels if c.get("kind") == "email"]
    wa_jids = [c["value"] for c in channels if c.get("kind") == "whatsapp"]

    counts = {"emails": 0, "whatsapp": 0, "documents": 0, "calendar": 0}
    raw_name = (contact.get("display_name") or "").strip()
    seeds: List[str] = []
    if raw_name and len(raw_name) >= 3:
        seeds.append(raw_name)

    try:
        with get_conn() as conn:
            if email_addrs:
                ph = ",".join("?" * len(email_addrs))
                counts["emails"] = conn.execute(
                    f"SELECT COUNT(*) AS n FROM email_messages WHERE from_addr IN ({ph})",
                    email_addrs,
                ).fetchone()["n"]
            if wa_jids:
                ph = ",".join("?" * len(wa_jids))
                counts["whatsapp"] = conn.execute(
                    f"SELECT COUNT(*) AS n FROM wa_messages "
                    f"WHERE chat_jid IN ({ph}) AND text IS NOT NULL AND text != ''",
                    wa_jids,
                ).fetchone()["n"]
                if _looks_like_bare_phone(raw_name):
                    pn = conn.execute(
                        f"SELECT push_name FROM wa_messages "
                        f"WHERE chat_jid IN ({ph}) AND push_name IS NOT NULL "
                        f"AND push_name != '' "
                        f"ORDER BY timestamp DESC LIMIT 1",
                        wa_jids,
                    ).fetchone()
                    if pn and pn["push_name"]:
                        seeds.append(pn["push_name"].strip())
            if seeds:
                # Cheap calendar count; doc count we approximate via FTS
                # k=10 (small) to avoid heavy work in a per-render hook.
                like_clauses = []
                params: List[Any] = []
                for seed in seeds:
                    like_clauses.append("(person = ? OR title LIKE ? OR notes LIKE ?)")
                    params.extend([seed, f"%{seed}%", f"%{seed}%"])
                cal_sql = "SELECT COUNT(*) AS n FROM events WHERE " + " OR ".join(like_clauses)
                counts["calendar"] = conn.execute(cal_sql, params).fetchone()["n"]
    except Exception as exc:  # noqa: BLE001
        log.debug("enricher: count summary failed for %s: %s", contact_id, exc)

    # Document count via Paperless FTS — bounded, best-effort
    if seeds:
        try:
            from . import paperless_ingest as _pp
            seen = set()
            for seed in seeds:
                fts = _pp.search_fts(seed, k=MAX_DOCS_PER_CONTACT * 2)
                for d in fts or []:
                    seen.add(d.get("paperless_doc_id"))
            counts["documents"] = len(seen)
        except Exception as exc:  # noqa: BLE001
            log.debug("enricher: count doc lookup failed for %s: %s", contact_id, exc)

    return {**counts, "seeds": seeds}


# ─── LLM prompt + parse ───────────────────────────────────────────────

def _build_prompt(digest: Dict[str, Any]) -> List[Dict[str, str]]:
    """Build the LLM prompt for one contact. The contact's current
    fields are shown so the LLM doesn't re-propose things already on
    file, and trust-tier hints push it toward formal docs over chat
    mentions."""
    contact = digest.get("contact", {})
    current = contact.get("current_fields") or {}
    name = contact.get("display_name") or "(unnamed)"

    body_lines: List[str] = [
        f"You are filling in missing contact info for: {name}",
        "",
        "ALREADY KNOWN (don't re-propose unless you have a different value):",
        json.dumps(current, ensure_ascii=False, indent=2) if current else "  (nothing yet)",
        "",
        "TRUST RANKING when sources disagree:",
        "  1. Paperless document (contract, invoice, official letter) — high",
        "  2. Email signature (last few lines of an email body) — medium-high",
        "  3. Email body mention — medium",
        "  4. Calendar event notes — medium (location often partial: 'Hannover', 'Hauptstr')",
        "  5. WhatsApp message — low (often casual / about a third party)",
        "",
        "RULES:",
        "  - Only propose values you can CITE — every proposal must reference a source.",
        "  - Don't propose 'I'll be at Munich office next week' as their permanent address.",
        "  - Partial addresses ARE useful — if only the city is known, propose just {city: 'Hannover'}; the user can fill in street+postcode later.",
        "  - DISAMBIGUATION: if the contact's name is common (e.g. 'Sara', 'Andreas'), a document mention may be about a DIFFERENT person with the same name. Only propose values when the source clearly refers to THIS contact — match other known facts (email address, phone, existing address) before trusting a name match.",
        "  - Birthday: only if the source clearly states a DOB or 'my birthday is X'.",
        "  - Relation: classify as 'family' | 'friend' | 'colleague' | 'vendor' | 'customer' | 'service_provider' based on the relationship signal.",
        "  - kind: 'business' if the contact represents a company; 'person' otherwise.",
        "  - Empty output is fine — better to skip than to guess.",
        "",
    ]

    if digest.get("addresses"):
        body_lines.append("ADDRESSES ALREADY ON FILE:")
        for a in digest["addresses"]:
            body_lines.append(f"  • {a.get('line1','')}, {a.get('postcode','')} {a.get('city','')}, {a.get('country','')}")
        body_lines.append("")

    if digest.get("emails"):
        body_lines.append("─── EMAILS (most recent first) ───")
        for i, e in enumerate(digest["emails"], 1):
            body_lines.append(f"[email-{i}] Subject: {e.get('subject', '')}")
            body_lines.append(f"  From: {e.get('from', '')}")
            body_lines.append(f"  Body: {e.get('body', '')}")
            body_lines.append("")

    if digest.get("whatsapp"):
        body_lines.append("─── WHATSAPP MESSAGES (most recent first) ───")
        for i, m in enumerate(digest["whatsapp"], 1):
            who = "USER" if m.get("from_me") else "THEM"
            body_lines.append(f"[wa-{i}] {who}: {m.get('text', '')}")
        body_lines.append("")

    if digest.get("documents"):
        body_lines.append("─── PAPERLESS DOCUMENTS MENTIONING THIS CONTACT ───")
        for i, d in enumerate(digest["documents"], 1):
            body_lines.append(f"[doc-{i}] {d.get('title', '')}  (paperless_id={d.get('doc_id')})")
            body_lines.append(f"  Excerpt: {d.get('snippet', '')}")
            body_lines.append("")

    if digest.get("calendar"):
        body_lines.append("─── CALENDAR EVENTS INVOLVING THIS CONTACT ───")
        body_lines.append("(Past meetings + visits often leak partial addresses — even just a city is useful.)")
        for i, e in enumerate(digest["calendar"], 1):
            body_lines.append(f"[cal-{i}] {e.get('title', '')}  ({e.get('when', '')})")
            if e.get("details"):
                body_lines.append(f"  Notes: {e['details']}")
            body_lines.append("")

    body_lines.extend([
        "─── TASK ───",
        "Return a JSON object with this exact shape:",
        '  {"proposals": [',
        '     {"field": "<field_name>",',
        '      "value": <string OR (for address only) object {line1,line2,postcode,city,country}>,',
        '      "confidence": <0.0..1.0>,',
        '      "source_kind": "email_signature"|"email_body"|"whatsapp"|"paperless_doc"|"calendar_event"|"manual",',
        '      "source_ref":  "<email-N|wa-N|doc-N|cal-N|null>",',
        '      "source_snippet": "<short text that justifies this value>"',
        '     }, ...',
        '  ]}',
        "",
        f"Valid field names: {', '.join(ENRICHABLE_FIELDS)}",
        "Multiple proposals for the same field ARE allowed (e.g. two addresses found).",
        "Return only the JSON object, no markdown fences, no commentary.",
    ])

    return [{
        "role": "system",
        "content": ("You extract structured contact info from messy text. Output strict "
                    "JSON only. Cite a source for every proposal. Skip rather than guess."),
    }, {
        "role": "user",
        "content": "\n".join(body_lines),
    }]


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_llm_proposals(text: str) -> List[Dict[str, Any]]:
    """Extract the proposals list from the LLM's JSON. Tolerates a
    surrounding ```json fence or some prose around the object. Drops
    proposals with unknown fields or missing required keys."""
    if not text:
        return []
    m = _JSON_OBJ_RE.search(text)
    raw = m.group(0) if m else text.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("enricher: couldn't parse LLM response as JSON: %r", text[:200])
        return []
    proposals = parsed.get("proposals") if isinstance(parsed, dict) else None
    if not isinstance(proposals, list):
        return []
    out: List[Dict[str, Any]] = []
    for p in proposals:
        if not isinstance(p, dict):
            continue
        field = p.get("field")
        value = p.get("value")
        if field not in ENRICHABLE_FIELDS:
            continue
        if value is None or value == "":
            continue
        # address values arrive as dicts → serialize to JSON for storage
        if field == "address" and isinstance(value, dict):
            value_str = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, (str, int, float)):
            value_str = str(value).strip()
        else:
            continue
        if not value_str:
            continue
        out.append({
            "field":          field,
            "value":          value_str,
            "confidence":     max(0.0, min(1.0, float(p.get("confidence") or 0.5))),
            "source_kind":    str(p.get("source_kind") or "manual"),
            "source_ref":     str(p.get("source_ref") or "") or None,
            "source_snippet": (str(p.get("source_snippet") or ""))[:500] or None,
        })
    return out


# ─── DB write ─────────────────────────────────────────────────────────

def _replace_pending_proposals(contact_id: int, proposals: List[Dict[str, Any]]) -> int:
    """Wipe existing PENDING proposals for this contact and insert the
    fresh batch. Accepted/rejected proposals (the user's prior
    decisions) are kept so re-running doesn't re-suggest them."""
    from .database import get_conn
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM contact_enrichment_proposals "
            "WHERE contact_id=? AND status='pending'",
            (contact_id,),
        )
        for p in proposals:
            conn.execute(
                "INSERT INTO contact_enrichment_proposals "
                "(contact_id, field_name, proposed_value, confidence, "
                " source_kind, source_ref, source_snippet) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (contact_id, p["field"], p["value"], p["confidence"],
                 p["source_kind"], p["source_ref"], p["source_snippet"]),
            )
        conn.commit()
    return len(proposals)


# ─── Orchestration ────────────────────────────────────────────────────

def enrich_one(contact_id: int, *, llm_client: Any = None) -> Dict[str, Any]:
    """Run the enricher on a single contact. Returns a small summary
    dict {contact_id, proposals_written, sources_scanned, error?}.

    Heartbeats the same `contact_enricher` worker that enrich_all uses
    so the header Enrich pill shows activity during single-contact
    runs too (UI was otherwise silent for the 5-30s LLM call)."""
    workers.register("contact_enricher", kind="batch", expected_interval_s=86400)
    workers.heartbeat("contact_enricher", "ok", f"contact {contact_id}: gathering mentions…")
    digest = _collect_contact_mentions(contact_id)
    if not digest:
        workers.heartbeat("contact_enricher", "warn", f"contact {contact_id}: not found")
        return {"contact_id": contact_id, "proposals_written": 0, "error": "contact not found"}
    sources_scanned = {
        "emails":    len(digest.get("emails", [])),
        "whatsapp":  len(digest.get("whatsapp", [])),
        "documents": len(digest.get("documents", [])),
        "calendar":  len(digest.get("calendar", [])),
    }
    has_data = sum(sources_scanned.values()) > 0
    if not has_data:
        workers.heartbeat("contact_enricher", "ok",
                          f"contact {contact_id}: no mentions in emails/wa/docs/cal")
        return {"contact_id": contact_id, "proposals_written": 0,
                "sources_scanned": sources_scanned,
                "note": "no mentions found in emails, WhatsApp, documents, or calendar"}

    if llm_client is None:
        from .agent.llm import LlmClient
        from . import ask as _ask
        llm_client = LlmClient(model=_ask.LLM_MODEL, base_url=_ask.LLM_BASE_URL)

    workers.heartbeat(
        "contact_enricher", "ok",
        f"contact {contact_id}: asking LLM "
        f"({sources_scanned['emails']}e/{sources_scanned['whatsapp']}wa/"
        f"{sources_scanned['documents']}d/{sources_scanned['calendar']}cal)",
    )
    messages = _build_prompt(digest)
    try:
        resp = llm_client.chat(messages, temperature=0.1, max_tokens=2000)
        text = (resp.get("content") or "") if isinstance(resp, dict) else str(resp)
    except Exception as exc:  # noqa: BLE001
        log.warning("enricher: LLM call failed for contact %s: %s", contact_id, exc)
        workers.heartbeat("contact_enricher", "error", f"contact {contact_id}: LLM failed — {exc}")
        return {"contact_id": contact_id, "proposals_written": 0,
                "sources_scanned": sources_scanned, "error": str(exc)}

    proposals = _parse_llm_proposals(text)
    written = _replace_pending_proposals(contact_id, proposals)
    workers.heartbeat(
        "contact_enricher", "ok",
        f"contact {contact_id}: {written} proposals from "
        f"{sources_scanned['emails']}e/{sources_scanned['whatsapp']}wa/"
        f"{sources_scanned['documents']}d/{sources_scanned['calendar']}cal",
    )
    return {"contact_id": contact_id, "proposals_written": written,
            "sources_scanned": sources_scanned}


def enrich_all() -> Dict[str, Any]:
    """Walk every contact (active + pending; skip spam/archived) and
    enrich. Heartbeats progress per contact for the Settings panel.
    Cooperative cancel — checked once per contact."""
    workers.register("contact_enricher", kind="batch", expected_interval_s=86400)
    workers.heartbeat("contact_enricher", "starting", "loading contacts")
    _reset_cancel()

    from .contacts import conn_ctx as _ccx
    with _ccx() as c:
        rows = c.execute(
            "SELECT id, display_name FROM contacts "
            "WHERE status IN ('active', 'pending') "
            "ORDER BY last_used_at DESC NULLS LAST, id"
        ).fetchall()
    total = len(rows)

    from .agent.llm import LlmClient
    from . import ask as _ask
    client = LlmClient(model=_ask.LLM_MODEL, base_url=_ask.LLM_BASE_URL)

    summary = {
        "total":       total,
        "enriched":    0,
        "skipped":     0,
        "failed":      0,
        "proposals":   0,
        "started_at":  time.time(),
    }
    for r in rows:
        if is_cancel_requested():
            elapsed = int(time.time() - summary["started_at"])
            workers.heartbeat(
                "contact_enricher", "ok",
                f"STOPPED at {summary['enriched']}/{total} after {elapsed}s · "
                f"{summary['proposals']} proposals · {summary['failed']} failed",
            )
            summary["cancelled"] = True
            summary["elapsed_s"] = elapsed
            return summary

        cid = int(r["id"])
        result = enrich_one(cid, llm_client=client)
        if result.get("error"):
            summary["failed"] += 1
        elif result.get("proposals_written", 0) > 0:
            summary["enriched"] += 1
            summary["proposals"] += result["proposals_written"]
        else:
            summary["skipped"] += 1

        done = summary["enriched"] + summary["skipped"] + summary["failed"]
        workers.heartbeat(
            "contact_enricher", "ok",
            f"{done}/{total} · {summary['enriched']} enriched · "
            f"{summary['proposals']} proposals · {summary['failed']} failed",
        )

    elapsed = int(time.time() - summary["started_at"])
    workers.heartbeat(
        "contact_enricher", "ok",
        f"DONE in {elapsed}s · {summary['enriched']}/{total} enriched · "
        f"{summary['proposals']} proposals · {summary['failed']} failed",
    )
    summary["elapsed_s"] = elapsed
    return summary
