"""Inline audit hooks + per-turn ContextVars.

Yorik's old SqlCapturingAuditLogger subclassed Vanna's AuditLogger
abstract base class to capture three signals during the agent loop:

1. **Last SQL string** — read after the loop by the response builder
   to surface ``sql_used`` in the API reply.
2. **Whether a mutation skill fired** — read by the cache_save gate
   to refuse to cache responses that mutated the DB.
3. **How many deletes fired this turn** — read by the delete-skill
   guardrails to refuse the 2nd+ delete in one turn (after the
   "deleted all of yesterday's babysitter instead of the one"
   incident).

The new agent loop has no abstract AuditLogger — it calls these
functions directly at the right points. Same semantics, no inheritance
plumbing.

All three are ContextVars so they're per-async-task and never leak
between concurrent requests.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# ContextVars — per-turn state
# ---------------------------------------------------------------------------

# Last SQL string executed via run_sql this turn. Surface in the API
# response so the chat UI can show "ran: SELECT id, title FROM events ..."
# under the assistant message.
_last_sql_for_request: ContextVar[Optional[str]] = ContextVar(
    "_last_sql_for_request", default=None,
)

# True if the LLM invoked any skill in MUTATION_SKILLS this turn. The
# cache_save gate refuses to cache a response when this is True, so a
# "Hab den Termin verschoben" success message can never be replayed
# without re-running the underlying mutation. THE CRITICAL FIX from the
# fabricated-success investigation — do not remove without re-running
# the voice eval.
_mutation_skill_invoked: ContextVar[bool] = ContextVar(
    "_mutation_skill_invoked", default=False,
)

# How many destructive-mutation skill calls fired this turn. The delete
# skills check this counter and refuse the 2nd+ call so an ambiguous
# voice command can't wipe a whole day of data.
_deletes_this_turn: ContextVar[int] = ContextVar(
    "_deletes_this_turn", default=0,
)


# ---------------------------------------------------------------------------
# Skill classifications
# ---------------------------------------------------------------------------

# Skills that mutate persistent state. The cache-gate fires for any of
# these; the delete-throttle is separate (only the `delete_*` subset).
# Add new mutating skills here as they're written, or the cache will
# start replaying their fabricated successes.
MUTATION_SKILLS: frozenset[str] = frozenset({
    "add_calendar_event", "update_calendar_event", "delete_calendar_event",
    "add_task", "update_task", "delete_task",
    "add_bill", "update_bill", "delete_bill",
    "compose_draft", "email_draft", "whatsapp_draft",
    # Contacts (identity hub) — every mutating one disables the
    # response-cache so a fabricated "Hab Oma gespeichert" never replays
    # without re-running the skill.
    "add_contact", "update_contact", "delete_contact",
    "add_contact_channel", "add_contact_address",
    "promote_pending_contact", "mark_contact_spam",
})

# Destructive skills (the per-turn throttle's domain). Currently the
# `delete_*` family; could grow if we add e.g. "purge_old_events".
DESTRUCTIVE_SKILLS: frozenset[str] = frozenset({
    "delete_calendar_event", "delete_task", "delete_bill",
    "delete_contact",
})

# Tools whose result we want to capture as ``sql_used`` for the API reply.
SQL_CAPTURING_TOOLS: frozenset[str] = frozenset({"run_sql", "RunSqlTool"})

# How many destructive ops are allowed per /api/ask turn before the next
# one is refused. Currently 1 — keeps voice-driven deletes safe.
DELETE_TURN_LIMIT = 1


# ---------------------------------------------------------------------------
# Inline hooks the loop calls
# ---------------------------------------------------------------------------


def reset_turn() -> None:
    """Reset all per-turn ContextVars. Loop calls this at the top of every
    /api/ask before the LLM dispatch loop starts. Caches the new tokens
    via set() — calls to .get() in this async task from here on see the
    reset state."""
    _last_sql_for_request.set(None)
    _mutation_skill_invoked.set(False)
    _deletes_this_turn.set(0)


def record_tool_call(name: str, args: Dict[str, Any]) -> None:
    """Inline hook fired by the loop right before a tool dispatch.

    For ``run_sql`` we capture the SQL string. For ``use_skill`` whose
    inner ``name`` is a mutation skill, we flip the cache gate (and
    increment the destructive counter when it's a delete skill).
    """
    if name in SQL_CAPTURING_TOOLS:
        sql = (args or {}).get("sql") or (args or {}).get("query")
        if isinstance(sql, str) and sql.strip():
            _last_sql_for_request.set(sql)
        return

    if name == "use_skill":
        inner = (args or {}).get("name")
        if isinstance(inner, str):
            if inner in MUTATION_SKILLS:
                _mutation_skill_invoked.set(True)
            if inner in DESTRUCTIVE_SKILLS:
                _deletes_this_turn.set(_deletes_this_turn.get() + 1)


# ---------------------------------------------------------------------------
# Read accessors (used by cache.py + the loop's response builder)
# ---------------------------------------------------------------------------


def last_sql() -> Optional[str]:
    return _last_sql_for_request.get()


def mutation_skill_invoked() -> bool:
    return _mutation_skill_invoked.get()


def deletes_this_turn() -> int:
    return _deletes_this_turn.get()


def increment_deletes_this_turn() -> int:
    """For the delete-skill guardrail to bump after passing its check.

    Returns the new value. (Kept as a separate explicit call rather than
    bundled into record_tool_call so the gate check + increment happen
    in a single place inside each delete skill.)
    """
    n = _deletes_this_turn.get() + 1
    _deletes_this_turn.set(n)
    return n


__all__ = [
    "MUTATION_SKILLS",
    "DESTRUCTIVE_SKILLS",
    "SQL_CAPTURING_TOOLS",
    "DELETE_TURN_LIMIT",
    "reset_turn",
    "record_tool_call",
    "last_sql",
    "mutation_skill_invoked",
    "deletes_this_turn",
    "increment_deletes_this_turn",
]
