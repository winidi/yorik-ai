"""Suggestion engine — the dispatch loop.

Single entry point: `analyse_message(owner_user_id, source_kind,
source_id)`. Everything else is the toggle hierarchy + registry
iteration. Adding a new modality / suggestion type / retriever is a
register() call elsewhere; this file never grows.

Day 1 state: skeleton wires every layer (toggle checks → contact
resolution → retriever dispatch → LLM stub → persistence) but the
LLM call returns no suggestions. By Day 3 the LLM is live and emits
real cards.

Always-safe behaviour:
* When toggles are off → silent no-op, no rows created, no LLM call.
* When the LLM is unreachable / errors → suggestion_run logged as
  'error', zero suggestions persisted. User sees nothing rather
  than a broken/fake card.
* Exceptions in a single retriever do NOT kill the whole run; the
  others still contribute evidence. Suggestion engine is "best
  effort" by design — a paperless retriever crashing shouldn't
  swallow Anna's reply suggestion.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import registry as _reg
from .registry import Evidence, RetrieverContext, HandlerContext

log = logging.getLogger("yorik.suggestions.engine")


# ─── Helpers — toggle hierarchy ─────────────────────────────────────

@dataclass
class _UserPrefs:
    suggestions_enabled: bool
    source_enabled:      bool


def _load_user_prefs(conn, owner_user_id: str, source_kind: str) -> _UserPrefs:
    """Read the user's global + per-source toggles. Defaults to OFF
    on missing row / parse failure — safer to skip than to silently
    enable analysis."""
    try:
        row = conn.execute(
            "SELECT suggestions_enabled, suggestion_sources FROM user_profiles WHERE id=?",
            (owner_user_id,),
        ).fetchone()
        if not row:
            return _UserPrefs(False, False)
        master = bool(row["suggestions_enabled"])
        sources = row["suggestion_sources"] or {}
        # JSONB comes back as a dict on psycopg; fall back to JSON
        # parse for any odd transport.
        if isinstance(sources, str):
            try:
                sources = json.loads(sources)
            except json.JSONDecodeError:
                sources = {}
        source_ok = bool((sources or {}).get(source_kind, False))
        return _UserPrefs(master, source_ok)
    except Exception as exc:  # noqa: BLE001
        log.debug("toggle-load failed for user %s: %s", owner_user_id, exc)
        return _UserPrefs(False, False)


def _resolve_contact_for_source(
    conn,
    owner_user_id: str,
    source_kind: str,
    source_row: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Find the canonical contact for the sender of this message.
    Modality-specific lookup keys; falls back to None when no contact
    matches (engine then skips with reason='no_contact')."""
    if source_kind == "email":
        addr = (source_row.get("from_email") or "").strip().lower()
        if not addr:
            return None
        r = conn.execute(
            "SELECT c.id, c.display_name, c.yorik_assist_enabled "
            "FROM contacts c "
            "JOIN contact_channels ch ON ch.contact_id = c.id "
            "WHERE ch.kind='email' AND LOWER(ch.value)=? LIMIT 1",
            (addr,),
        ).fetchone()
        return dict(r) if r else None
    if source_kind == "wa":
        jid = (source_row.get("chat_jid") or "").strip()
        if not jid:
            return None
        r = conn.execute(
            "SELECT c.id, c.display_name, c.yorik_assist_enabled "
            "FROM contacts c "
            "JOIN contact_channels ch ON ch.contact_id = c.id "
            "WHERE ch.kind='whatsapp' AND ch.value=? LIMIT 1",
            (jid,),
        ).fetchone()
        return dict(r) if r else None
    return None


def _load_source_row(conn, source_kind: str, source_id: int) -> Optional[Dict[str, Any]]:
    """Fetch the source message row keyed by (source_kind, source_id).
    Retrievers + the LLM prompt builder use this — the engine doesn't
    care about the contents, just hands it through."""
    if source_kind == "email":
        r = conn.execute(
            "SELECT m.id, m.subject, m.snippet, m.body_text, m.body_html, "
            "       m.from_email, m.from_name, m.to_addrs, m.date_received, "
            "       m.message_id, m.thread_id, m.account_id, m.owner_user_id "
            "FROM email_messages m WHERE m.id=?",
            (source_id,),
        ).fetchone()
        return dict(r) if r else None
    if source_kind == "wa":
        r = conn.execute(
            "SELECT msg_id, chat_jid, from_me, push_name, timestamp, text, "
            "       transcript, owner_user_id "
            "FROM wa_messages WHERE msg_id=? LIMIT 1",
            (str(source_id),),
        ).fetchone()
        return dict(r) if r else None
    return None


# ─── Run-tracking persistence ───────────────────────────────────────

def _open_run(conn, owner_user_id: str, source_kind: str, source_id: int,
              contact_id: Optional[int]) -> int:
    cur = conn.execute(
        "INSERT INTO suggestion_runs "
        "(owner_user_id, source_kind, source_id, contact_id, status) "
        "VALUES (?, ?, ?, ?, 'running') RETURNING id",
        (owner_user_id, source_kind, source_id, contact_id),
    )
    return int(cur.fetchone()["id"])


def _close_run(conn, run_id: int, status: str, *, error: Optional[str] = None,
               skip_reason: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE suggestion_runs "
        "SET status=?, finished_at=NOW(), error=?, skip_reason=? "
        "WHERE id=?",
        (status, error, skip_reason, run_id),
    )


def _skip(conn, owner_user_id: str, source_kind: str, source_id: int,
          contact_id: Optional[int], reason: str) -> None:
    """Log a skipped run for diagnostics — doesn't run retrievers /
    LLM. Useful for the activity view ('Yorik didn't analyse this
    because: master toggle off')."""
    rid = _open_run(conn, owner_user_id, source_kind, source_id, contact_id)
    _close_run(conn, rid, "skipped", skip_reason=reason)
    conn.commit()


# ─── Public entry point ─────────────────────────────────────────────

async def analyse_message(
    owner_user_id: str,
    source_kind: str,
    source_id: int,
    *,
    user_role: str = "admin",
) -> Dict[str, Any]:
    """Analyse one incoming message in the context of the sender's
    history. Returns a result dict (suggestion ids + summary). Never
    raises — failures are persisted to suggestion_runs.

    Designed to be called from a background task (Layer 3 hook in the
    email fetcher etc.); the synchronous return is small and safe to
    log."""
    from ..database import get_conn

    # Step 1: load source + contact for toggle checks.
    with get_conn() as conn:
        source_row = _load_source_row(conn, source_kind, source_id)
        if not source_row:
            _skip(conn, owner_user_id, source_kind, source_id, None, "source_missing")
            return {"status": "skipped", "reason": "source_missing"}

        prefs = _load_user_prefs(conn, owner_user_id, source_kind)
        if not prefs.suggestions_enabled:
            _skip(conn, owner_user_id, source_kind, source_id, None, "global_toggle_off")
            return {"status": "skipped", "reason": "global_toggle_off"}
        if not prefs.source_enabled:
            _skip(conn, owner_user_id, source_kind, source_id, None, f"source_off:{source_kind}")
            return {"status": "skipped", "reason": f"source_off:{source_kind}"}

        contact = _resolve_contact_for_source(conn, owner_user_id, source_kind, source_row)
        if not contact:
            _skip(conn, owner_user_id, source_kind, source_id, None, "no_contact")
            return {"status": "skipped", "reason": "no_contact"}
        if not contact.get("yorik_assist_enabled"):
            _skip(conn, owner_user_id, source_kind, source_id, int(contact["id"]), "contact_opt_out")
            return {"status": "skipped", "reason": "contact_opt_out"}

        # Open the run; finalize in finally so a crash mid-flight is
        # visible in the activity view instead of leaving a 'running'
        # row forever.
        run_id = _open_run(conn, owner_user_id, source_kind, source_id, int(contact["id"]))
        conn.commit()

    contact_id = int(contact["id"])

    # Step 2: run all retrievers in parallel. Each one's failure is
    # isolated — the rest still contribute evidence.
    ctx = RetrieverContext(
        owner_user_id=owner_user_id,
        source_kind=source_kind,
        source_id=source_id,
        contact_id=contact_id,
        source_row=source_row,
    )
    retrievers = _reg.get_retrievers(scope="message")
    evidence: List[Evidence] = []
    if retrievers:
        async def _safe(r):
            try:
                return await r.fetch(ctx)
            except Exception as exc:  # noqa: BLE001
                log.warning("retriever %s failed: %s", r.name, exc)
                return []
        results = await asyncio.gather(*[_safe(r) for r in retrievers])
        for batch in results:
            evidence.extend(batch or [])

    # Step 3: LLM call. Day 1 stub — returns empty. Day 2 wires the
    # real Qwen call with structured output constrained to registered
    # suggestion types.
    suggestions = await _call_llm_for_suggestions(
        source_row=source_row,
        contact=contact,
        evidence=evidence,
    )

    # Step 4: persist + close run.
    try:
        with get_conn() as conn:
            persisted_ids: List[int] = []
            for s in suggestions:
                # Day 3 will add validate() pre-emit gate + evidence
                # ref-id checks. Day 1 just persists what the stub
                # gave us (which is nothing).
                cur = conn.execute(
                    "INSERT INTO suggestions "
                    "(run_id, owner_user_id, type, payload_json, confidence, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                    (run_id, owner_user_id, s["type"], json.dumps(s.get("payload", {})),
                     s.get("confidence", "medium"), s.get("reason", "")),
                )
                sid = int(cur.fetchone()["id"])
                persisted_ids.append(sid)
                for ev in (s.get("evidence") or []):
                    conn.execute(
                        "INSERT INTO suggestion_evidence "
                        "(suggestion_id, kind, ref_id, ref_text, snippet) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (sid, ev.get("kind"), ev.get("ref_id"),
                         ev.get("ref_text"), (ev.get("snippet") or "")[:140]),
                    )
            _close_run(conn, run_id, "done")
            conn.commit()
        return {"status": "done", "suggestion_ids": persisted_ids,
                "count": len(persisted_ids), "evidence_count": len(evidence)}
    except Exception as exc:  # noqa: BLE001
        log.exception("suggestions: persist failed for run %s: %s", run_id, exc)
        try:
            with get_conn() as conn:
                _close_run(conn, run_id, "error", error=str(exc))
                conn.commit()
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "error": str(exc)}


async def _call_llm_for_suggestions(*, source_row: Dict[str, Any],
                                     contact: Dict[str, Any],
                                     evidence: List[Evidence]) -> List[Dict[str, Any]]:
    """LLM hook. Day 1 returns empty — the engine plumbing is fully
    wired but no LLM call is made yet. Day 2 fills this in with the
    real Qwen call + structured output validation.

    Returning [] cleanly = the engine persists zero suggestions but
    still records the run as 'done'. That's the right behaviour both
    on Day 1 and forever after, for messages the LLM doesn't think
    deserve a suggestion."""
    log.debug("LLM stub called for source %s — returning no suggestions",
              source_row.get("id"))
    return []
