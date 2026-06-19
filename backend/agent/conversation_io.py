"""Conversation persistence for the new agent loop.

Stores OpenAI-format messages (plain dicts) in the ``agent_conversations``
table — no Vanna Message Pydantic types, no model_dump/model_validate
round-trip. The new loop reads what it'll send and writes what it just
processed; the wire format is the storage format.

Coexists with the legacy ``conversations`` table (Vanna format) during
the cutover. Phase 4 drops the legacy table.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

from ..database import DEFAULT_DB_PATH, conn_ctx

logger = logging.getLogger("yorik.agent.conversation_io")

DB_PATH = os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)

# No implicit history cap. The previous 40-message default was a
# silent footgun: load_messages would keep only the last N messages,
# the agent loop would mutate that truncated list, and save_messages
# would overwrite the on-disk row with the truncated copy. The
# original user intent ("schreib eine Rechnung über 420 Euro …")
# vanished from disk as soon as the first turn produced more than
# 40 messages — which the pure-Hermes architecture (every invoke
# pairs with a skill_view) hits on turn 1 of any compose flow.
#
# Storage is now unbounded by default. Callers that genuinely need
# a bounded window for performance (e.g. compose_check_template_args
# scanning recent user turns for Bug 4 extraction) pass an explicit
# `limit=N`. Per-LLM-call truncation, if needed later, belongs at
# the LLM-feed seam (sanitize_for_llm), NOT at the storage boundary.
DEFAULT_HISTORY_LIMIT: Optional[int] = None


def load_messages(
    conversation_id: str,
    user_role: str,
    *,
    limit: Optional[int] = DEFAULT_HISTORY_LIMIT,
) -> List[Dict[str, Any]]:
    """Load the latest ``limit`` messages for a conversation.

    Returns an empty list when:
      - the conversation doesn't exist (first turn of a new thread),
      - the conversation exists but the requesting role doesn't match
        (access denied — mirrors the legacy store's behaviour).

    The returned list is in chronological order (oldest first) and
    PRESERVES storage extras (photos / documents / ui_actions /
    metadata / agent_trace) so the next save_messages keeps them on
    prior turns. Callers feeding the LLM must call sanitize_for_llm()
    on the messages immediately before each LLM request — otherwise
    strict OpenAI-spec providers reject the array.

    Previously this function stripped extras eagerly; that wiped
    every prior turn's photos as soon as the user typed a second
    message in the same conversation (the chat UI showed the earlier
    photo grid gone on revisit — surfaced as "I asked for photos
    of Anna, then for an appointment, and the photos vanished").
    """
    if not conversation_id:
        return []
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_role, messages_json FROM agent_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    if not row:
        return []
    if row["user_role"] != user_role:
        logger.debug(
            "conversation %s belongs to role=%r, refused for role=%r",
            conversation_id, row["user_role"], user_role,
        )
        return []
    try:
        msgs = json.loads(row["messages_json"]) or []
    except json.JSONDecodeError:
        logger.warning("corrupt messages_json on conversation %s — starting fresh", conversation_id)
        return []
    if not isinstance(msgs, list):
        return []
    if limit and len(msgs) > limit:
        msgs = msgs[-limit:]
    # Drop any role=system entries from history. The caller (loop.py)
    # ALWAYS prepends a fresh system_message at position 0. If a
    # legacy save persisted the system message inline, replaying it
    # would produce two system entries in the array — Qwen3's chat
    # template raises "System message must be at the beginning" on
    # that exact shape. Defence-in-depth alongside the save-side
    # filter so old conversations don't poison new turns.
    return [m for m in msgs if isinstance(m, dict) and m.get("role") != "system"]


def sanitize_for_llm(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a copy of ``messages`` with storage-only extras removed.

    Call this immediately before handing the list to an LLM client —
    storage extras (photos / documents / ui_actions / metadata /
    agent_trace) waste tokens and trip strict-provider validation.
    The original list is left intact so save_messages() persists
    with all extras still on prior assistant turns.
    """
    return [_strip_storage_extras(m) for m in (messages or [])
            if isinstance(m, dict)]


_OPENAI_MESSAGE_KEYS = frozenset({
    "role", "content", "name", "tool_calls", "tool_call_id",
    "function_call",  # legacy OpenAI shape
})


def _strip_storage_extras(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``msg`` with only OpenAI-recognised fields.

    Extras we routinely add (``metadata``, ``photos``, ``documents``,
    ``ui_actions``, ``agent_trace``) are for chat-UI rehydration; the
    LLM doesn't need them and strict providers reject them.
    """
    if not isinstance(msg, dict):
        return msg
    out: Dict[str, Any] = {k: v for k, v in msg.items() if k in _OPENAI_MESSAGE_KEYS}
    # Preserve at minimum role+content so we never produce an invalid message.
    if "role" not in out:
        out["role"] = msg.get("role", "user")
    return out


def save_messages(
    conversation_id: str,
    user_role: str,
    user_id: Optional[int],
    messages: List[Dict[str, Any]],
) -> None:
    """Insert-or-update the message log for this conversation.

    Idempotent: callers can pass the full message list every turn and we
    overwrite. Refuses to clobber a conversation owned by a different
    role (matches the legacy store).
    """
    if not conversation_id:
        return
    # Never persist the system message. loop.py rebuilds it from
    # _SYSTEM_PROMPT on every turn (so the date / language / speaker
    # context stays fresh) and prepends it at position 0. Saving
    # the system message would make the next load_messages return
    # it as history, the loop would prepend another, and Qwen3's
    # chat template rejects the array with "System message must be
    # at the beginning." Mirror in load_messages for old conversations.
    persisted = [
        m for m in (messages or [])
        if isinstance(m, dict) and m.get("role") != "system"
    ]
    blob = json.dumps(persisted, ensure_ascii=False, default=str)
    with conn_ctx(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT user_role FROM agent_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if existing and existing["user_role"] != user_role:
            logger.warning(
                "refused to overwrite conversation %s (owner=%r, attempted by=%r)",
                conversation_id, existing["user_role"], user_role,
            )
            return
        if existing:
            conn.execute(
                "UPDATE agent_conversations SET messages_json = ?, "
                "user_id = COALESCE(?, user_id), "
                "updated_at = datetime('now') WHERE id = ?",
                (blob, user_id, conversation_id),
            )
        else:
            conn.execute(
                "INSERT INTO agent_conversations "
                "(id, user_role, user_id, messages_json) VALUES (?, ?, ?, ?)",
                (conversation_id, user_role, user_id, blob),
            )


def load_ledger(conversation_id: str, user_role: str) -> Dict[str, Any]:
    """Per-conversation entity ledger (see entity_ledger.py).

    Returns an empty dict for new conversations or when the requesting
    role doesn't own this conversation. Schema-tolerant: works against
    DBs that haven't run migration 035 yet by treating the column-missing
    error as 'empty ledger'.
    """
    if not conversation_id:
        return {}
    try:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT user_role, ledger_json FROM agent_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row or row["user_role"] != user_role:
        return {}
    try:
        data = json.loads(row["ledger_json"] or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def save_ledger(conversation_id: str, ledger: Dict[str, Any]) -> None:
    """Persist the ledger for this conversation. Must run AFTER
    save_messages so the row is guaranteed to exist. Schema-tolerant
    the same way load_ledger is."""
    if not conversation_id:
        return
    blob = json.dumps(ledger or {}, ensure_ascii=False)
    try:
        with conn_ctx(DB_PATH) as conn:
            conn.execute(
                "UPDATE agent_conversations SET ledger_json = ? WHERE id = ?",
                (blob, conversation_id),
            )
    except sqlite3.OperationalError:
        return  # migration 035 not yet applied — silent no-op


def save_message_trace(
    conversation_id: str,
    message_idx: int,
    trace: Optional[Dict[str, Any]],
) -> None:
    """Persist a per-message agent_trace blob (dev mode).

    Called by the loop after a turn ends, with ``message_idx`` pointing
    at the FINAL assistant message in the saved messages_json. Safe to
    call repeatedly — INSERT OR REPLACE keys on (conv_id, msg_idx).

    No-op when ``trace`` is None or the conversation doesn't exist yet
    (the FK would reject). Callers always save_messages() first.
    """
    if not conversation_id or not trace:
        return
    blob = json.dumps(trace, ensure_ascii=False, default=str)
    try:
        with conn_ctx(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_message_traces "
                "(conversation_id, message_idx, trace_json) "
                "VALUES (?, ?, ?)",
                (conversation_id, message_idx, blob),
            )
    except sqlite3.IntegrityError as exc:
        # FK violation (conv not yet committed) — silently skip; the
        # trace just won't be there on reload, which is fine for dev tooling.
        logger.debug("trace save skipped for %s/#%d: %s", conversation_id, message_idx, exc)


def load_traces_for(conversation_id: str) -> Dict[int, Dict[str, Any]]:
    """Load all persisted traces for a conversation, keyed by message_idx.

    Returns an empty dict if no traces exist. The GET conversations
    endpoint calls this once per request and attaches the matching
    blob to each message in its response.
    """
    if not conversation_id:
        return {}
    out: Dict[int, Dict[str, Any]] = {}
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT message_idx, trace_json FROM agent_message_traces "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchall()
    for r in rows:
        try:
            out[int(r["message_idx"])] = json.loads(r["trace_json"])
        except (ValueError, json.JSONDecodeError):
            continue
    return out


def delete_conversation(conversation_id: str, user_role: str) -> bool:
    """Drop a conversation. Returns True if deleted, False if not found
    or wrong role."""
    if not conversation_id:
        return False
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_role FROM agent_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if not row or row["user_role"] != user_role:
            return False
        conn.execute("DELETE FROM agent_conversations WHERE id = ?", (conversation_id,))
    return True


# ─── attachment stash ──────────────────────────────────────────────
# The chat-bound list of {url, filename, mimetype} pointers a user has
# accumulated while talking with Yorik ("attach this photo, and that
# document — actually that one too"). The email Composer reads it
# verbatim and fetches each URL on send. Schema-tolerant against DBs
# that haven't run migration 041 yet (silent empty list / no-op save).

# Cap to keep the column small and the email Composer responsive
# (each item triggers a fetch). 50 items at ~25 MB/each would blow
# past the 25 MB SMTP cap anyway — this is just a guard rail.
STASH_MAX_ITEMS = 50


def load_stash(conversation_id: str, user_role: str) -> List[Dict[str, Any]]:
    """Per-conversation attachment stash. Empty list for new threads,
    wrong-role accesses, or DBs pre-migration-041."""
    if not conversation_id:
        return []
    try:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT user_role, attachment_stash FROM agent_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return []
    if not row or row["user_role"] != user_role:
        return []
    try:
        data = json.loads(row["attachment_stash"] or "[]")
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def save_stash(
    conversation_id: str,
    user_role: str,
    stash: List[Dict[str, Any]],
) -> None:
    """Replace the stash with a caller-cleaned list. Truncates to the
    cap and refuses if the caller's role doesn't own the conversation.
    """
    if not conversation_id:
        return
    capped = list(stash or [])[:STASH_MAX_ITEMS]
    blob = json.dumps(capped, ensure_ascii=False)
    try:
        with conn_ctx(DB_PATH) as conn:
            row = conn.execute(
                "SELECT user_role FROM agent_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not row:
                # No conversation row yet — first turn hasn't been
                # saved. Caller (POST /stash) treats this as a 404.
                logger.debug("save_stash: conversation %s not found", conversation_id)
                return
            if row["user_role"] != user_role:
                logger.warning(
                    "refused stash write on conversation %s (owner=%r, by=%r)",
                    conversation_id, row["user_role"], user_role,
                )
                return
            conn.execute(
                "UPDATE agent_conversations SET attachment_stash = ? WHERE id = ?",
                (blob, conversation_id),
            )
    except sqlite3.OperationalError:
        return  # migration 041 not yet applied


__all__ = [
    "load_messages",
    "sanitize_for_llm",
    "save_messages",
    "delete_conversation",
    "save_message_trace",
    "load_traces_for",
    "load_stash",
    "save_stash",
]
