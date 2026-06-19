"""search_documents skill — hybrid RAG over Paperless + Yorik native uploads.

Ported from the legacy ui_tools.SearchDocumentsTool so the LLM sees one
unified surface (skills/*) instead of two (skills/* + ui_tools/*). The
audit caught the LLM calling `invoke_skill('search_documents', ...)`
per a system-prompt instruction and getting "Unknown skill" back —
because the old tool registered as a top-level Vanna-shaped tool, not
through the skills registry.

Behaviour preserved: dual-index merge (Paperless first, native fallback),
RRF on the search path, recent-N on the empty-query path, leg-status
caveats when one engine is down, documents_found UI emission for the
chat cards, and an anti-enumeration `_llm_hint` so the LLM doesn't list
every hit in prose.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

log = logging.getLogger("yorik.search_documents")


async def execute(ctx, query: str = "", k: int = 5) -> dict[str, Any]:
    role = getattr(ctx, "role", None) or "admin"
    user_id = getattr(ctx, "user_id", None)
    k = max(1, min(int(k or 5), 20))
    q = (query or "").strip()

    from backend import documents as documents_mod
    from backend import paperless_ingest as _pp
    from backend.ui_tools import _append

    # Phase C: restrict semantic search to the caller's visible spaces
    # so a workspace member can never receive chunks from a workspace
    # they don't belong to. When user_id is None (legacy / no-auth code
    # path), `visible_space_ids` stays None and search() preserves the
    # pre-Phase-C behaviour (no filter).
    visible_space_ids: List[int] | None = None
    if user_id is not None:
        try:
            from backend import spaces as _spaces
            visible_space_ids = _spaces.user_visible_space_ids(user_id, role)
        except Exception as exc:  # noqa: BLE001
            log.warning("user_visible_space_ids failed for user=%s role=%s: %s",
                        user_id, role, exc)

    if not q:
        # Empty-query path: recent N across both sources.
        try:
            native_hits = documents_mod.recent(k=k, role=role)
            native_total = len(documents_mod.list_documents(role=role))
        except Exception as exc:  # noqa: BLE001
            native_hits, native_total = [], 0
            log.warning("native recent failed: %s", exc)
        pp = _pp.recent_with_count(k=k)
        pp_hits = pp.get("hits", [])
        pp_total = pp.get("total", 0)
        hits = (pp_hits + native_hits)[:k]
        total = native_total + pp_total
        leg_caveat = ""
        sem_err = fts_err = None  # recent path has no leg failures
    else:
        # Search-query path: hybrid (semantic + Paperless FTS via RRF).
        try:
            native_hits = documents_mod.search(q, k=k, role=role)
            if native_hits and native_hits[0].get("ok") is False:
                native_hits = []
        except Exception as exc:  # noqa: BLE001
            native_hits = []
            log.warning("native search failed: %s", exc)
        try:
            hybrid = _pp.search_hybrid(q, k=k, visible_space_ids=visible_space_ids)
            pp_hits_raw = hybrid.get("hits", [])
            pp_legs = hybrid.get("legs", {})
        except Exception as exc:  # noqa: BLE001
            pp_hits_raw = []
            pp_legs = {}
            log.warning("paperless hybrid search failed: %s", exc)

        # Normalise paperless rows to the field names the rest of this
        # function uses: doc_id, chunk_text, doc_mime.
        pp_hits = [{
            "doc_id":      r.get("paperless_doc_id"),
            "doc_title":   r.get("doc_title") or f"Document {r.get('paperless_doc_id')}",
            "doc_mime":    "application/pdf",
            "chunk_text":  r.get("text") or "",
            "chunk_index": r.get("chunk_index", 0),
            "distance":    r.get("distance"),
            "match_type":  r.get("match_type"),
            "source":      "paperless",
        } for r in pp_hits_raw]
        hits = (pp_hits + native_hits)[:k]
        total = len(hits)

        sem_err = (pp_legs.get("semantic") or {}).get("error")
        fts_err = (pp_legs.get("fts") or {}).get("error")
        if sem_err and fts_err and not hits:
            leg_caveat = (
                f"Document store unreachable: semantic={sem_err}; "
                f"keyword={fts_err}. Tell the user Paperless / embeddings "
                "may be down; do NOT say 'no documents found'."
            )
        elif sem_err and fts_err:
            leg_caveat = f"Both engines reported issues. Semantic: {sem_err} Keyword: {fts_err}"
        elif sem_err:
            leg_caveat = f"Only keyword search ran. Semantic unavailable: {sem_err}"
        elif fts_err:
            leg_caveat = f"Only semantic search ran. Keyword unavailable: {fts_err}"
        else:
            leg_caveat = ""

    # Emit documents_found UI action so the chat renders cards. Dedupe
    # by doc_id, keep the best snippet.
    seen: Dict[int, Dict[str, Any]] = {}
    for h in hits:
        did = h.get("doc_id")
        if did is None or did in seen:
            continue
        snippet = (h.get("chunk_text") or h.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:237] + "…"
        seen[did] = {
            "doc_id":    did,
            "title":     h.get("doc_title") or h.get("title") or f"Document {did}",
            "mime_type": h.get("doc_mime") or h.get("mime_type"),
            "snippet":   snippet,
            "distance":  h.get("distance"),
            "source":    h.get("source") or "local",
        }
    if seen:
        _append({
            "type":      "documents_found",
            "query":     q,
            "documents": list(seen.values()),
        })

    # Surface the top match for the LLM to comment on. Hits are already
    # sorted: hits[0] is the best (paperless RRF + native cosine merged,
    # with paperless first since hybrid retrieval is usually stronger
    # than naive embeddings). We deliberately don't try to compute a
    # cross-leg confidence number — Paperless uses RRF composite ranks
    # and native uses raw cosine distance; they aren't comparable. Let
    # the LLM read the snippet and decide.
    top = next(iter(seen.values()), None)
    others_count = max(0, len(seen) - 1)

    # Anti-enumeration hint, plus a recent-vs-search caveat. Same shape
    # as check_tasks / find_known_provider so the LLM treats it the
    # same way.
    hint_parts: List[str] = []
    if hits and top and q and others_count > 0:
        # Search with multiple hits: lead with the top match the user
        # almost certainly meant, soft-frame it ("I think…"), then
        # acknowledge the rest without enumerating.
        hint_parts.append(
            f"shown_to_user:{len(seen)} document hit(s) rendered as cards in the chat. "
            f"Top match by relevance: doc_id={top['doc_id']} title={top['title']!r}. "
            f"Other candidates shown: {others_count}.\n"
            "Reply ONE short sentence in the user's language that:\n"
            "  (a) names the top match by title — e.g. 'Ich denke, du meinst "
            "<title>' / 'I think you mean <title>',\n"
            "  (b) acknowledges the others as a fallback — e.g. "
            "'die anderen Treffer sind unten, falls du etwas anderes meintest' / "
            "'others are below in case you meant something else'.\n"
            "Do NOT list the other doc titles, correspondents, dates, or doc_ids "
            "— the cards carry that. If the user then asks 'öffne den' / "
            "'open it', use read_document(doc_id=<top doc_id>) next.\n"
            "For value-seeking questions ('was habe ich für X bezahlt' / 'wie "
            "viel hat Y gekostet' / 'wann wurde Z unterschrieben'), read at "
            "LEAST 5 candidates via read_document and search across all for "
            "the value — the top match by relevance is often NOT the doc "
            "containing the answer; prefer Vertrag / Kalkulationsblatt / "
            "Rechnung over Auszahlungsbestätigung."
        )
    elif hits and top and q:
        # Single hit on a search query — no need to hedge.
        hint_parts.append(
            f"shown_to_user:1 document hit rendered as a card in the chat. "
            f"doc_id={top['doc_id']} title={top['title']!r}. "
            "Reply ONE short sentence in the user's language that names the "
            "title and confirms it's the only match — e.g. 'Ich hab nur <title> "
            "gefunden, passt das?' / 'Only <title> matched — is that the one?'. "
            "If the user confirms, use read_document(doc_id=<doc_id>) next."
        )
    elif hits:
        # Empty-query recent-N path: no "top match" framing — just a
        # neutral count, since recent ≠ relevant.
        n = len(seen)
        hint_parts.append(
            f"shown_to_user:{n} document hit(s) rendered as cards in the chat. "
            "Reply ONE short sentence in the user's language ('Drei Treffer, "
            "siehe Karten unten' / 'Three results, see cards below'). Do NOT "
            "list doc titles, correspondents, dates, or doc_ids in your text "
            "— the cards carry that."
        )
    elif not q:
        hint_parts.append(
            f"Library has {total} document(s); none surfaced for the recent-N view."
            if total else
            "No documents in the library yet. Suggest the user upload one in the Documents app."
        )
    else:
        hint_parts.append(
            f"No documents matched query={q!r}. Be honest about the empty "
            "result; do not invent hits. If the user clearly meant a "
            "different domain (bills / events / contacts), suggest the right skill."
        )
    if leg_caveat:
        hint_parts.append(leg_caveat)

    return {
        "hits":      list(seen.values()),
        "total":     total,
        "query":     q,
        "_llm_hint": "\n\n".join(hint_parts) if hint_parts else None,
    }
