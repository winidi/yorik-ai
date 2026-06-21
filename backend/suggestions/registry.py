"""Plugin contract for the suggestion engine.

THREE registries, all populated via import-time register() calls:

  retrievers  — fetch evidence about a sender/message from a modality
                (calendar events, prior emails, paperless docs, ...).
  types       — describe a suggestion the LLM can emit, plus the
                handler that runs when the user Accepts it.
  triggers    — what events should kick off an analyse_message run
                (new email arrived, new WA message, calendar invite
                received, ...).

The engine iterates these registries — it doesn't switch on hardcoded
modality names. Adding a new plugin = importing a module that calls
register(). No engine changes. This is the property that lets us add
WhatsApp / Telegram / paperless / future third-party addons without
ever touching the dispatch loop.

Type-safety: Pydantic models for the registered records. Handlers are
plain async callables — the engine validates inputs against schemas
before invoking. Bad data never reaches the handler.

Defaults: every contract field has a sensible default so the simplest
register call works:

    from .registry import register_retriever
    register_retriever(name="calendar", scope=["message"], fetch=my_fn)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

log = logging.getLogger("yorik.suggestions.registry")


# ─── Types we use everywhere ────────────────────────────────────────

@dataclass
class Evidence:
    """One reference the LLM can cite. Becomes a clickable chip in
    the suggestion card. kind/ref_id mirror suggestion_evidence
    table columns so persistence is a thin copy."""
    kind:     str           # 'calendar_event' | 'email_message' | 'task' | 'contact' | 'wa_message' | ...
    ref_id:   Optional[int] = None
    ref_text: Optional[str] = None  # for refs whose primary key is a string
    snippet:  str           = ""    # human-readable label for the chip


@dataclass
class RetrieverContext:
    """Everything a retriever gets when called. Engine fills in only
    what's relevant for the analysis — message_id when triggered by
    a message, contact_id always when a contact was resolved.

    Retrievers should be tolerant of missing fields; not every analysis
    has a calendar event id (etc.). Return [] when no evidence found.
    """
    owner_user_id: str
    source_kind:   str               # 'email' | 'wa' | ...
    source_id:     int
    contact_id:    Optional[int] = None
    # The full source-row dict, JSON-friendly. Retrievers that want
    # body text / subject lines / sender info read from here.
    source_row:    Dict[str, Any] = field(default_factory=dict)


FetchFn = Callable[[RetrieverContext], Awaitable[List[Evidence]]]


@dataclass
class ContextRetriever:
    """Registered retriever. The engine calls fetch() in parallel
    with every other retriever, then flattens the evidence into the
    LLM prompt context block."""
    name:  str                   # 'calendar' | 'email_history' | 'tasks' | ...
    scope: List[str]             # subset of ['message', 'contact']; engine
                                 # only calls when context matches
    fetch: FetchFn
    # When True, this retriever can be globally disabled by the user
    # in settings without breaking the engine. Defaults True — the
    # only "always-on" retriever is 'contact' which provides the
    # identity anchor every prompt needs.
    user_disable_ok: bool = True


# ─── Suggestion types ───────────────────────────────────────────────

HandlerFn = Callable[[Dict[str, Any], "HandlerContext"], Awaitable[Dict[str, Any]]]
ValidateFn = Callable[[Dict[str, Any], "HandlerContext"], Awaitable[bool]]


@dataclass
class HandlerContext:
    """Passed to suggestion-type handlers on Accept. Carries the user
    + the original suggestion row so the handler can update its
    status / dispatch to a skill / etc."""
    owner_user_id: str
    user_role:     str
    suggestion_id: int
    source_kind:   str
    source_id:     int
    contact_id:    Optional[int] = None


@dataclass
class SuggestionType:
    """Registered suggestion type. The LLM is told it can emit ONLY
    these types (via structured-output JSON schema), so a typo or a
    rogue type from a misbehaving prompt can't reach a handler.

    handler   runs on Accept. Returns a result dict that the UI shows.
    validate  optional pre-emit gate; engine calls it after the LLM
              returns and drops the suggestion if False. Use for hard
              constraints like 'don't suggest a slot that already
              has an event'.
    """
    type:           str                  # 'draft_reply' | 'propose_meeting_slot' | ...
    payload_schema: Dict[str, Any]       # JSON schema; passed to LLM as constraint
    handler:        HandlerFn
    validate:       Optional[ValidateFn] = None
    # Human-readable summary used in the UI card title when the LLM
    # didn't supply a `reason` (rare but defensive).
    fallback_title: str = ""


# ─── Triggers ───────────────────────────────────────────────────────

TriggerFn = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass
class Trigger:
    """A registered hook that fires analyse_message in response to
    an upstream event. MVP wires only 'email.new' (called from the
    email fetcher). Adding WhatsApp = registering 'wa.new' with a
    different on_fire — engine code doesn't change.

    on_fire receives the raw event payload (the same shape the
    upstream emitter uses). It's the trigger's job to extract a
    message_id and call engine.analyse_message itself. This keeps
    triggers small and engine API generic."""
    event:   str            # 'email.new' | 'wa.new' | 'calendar.invite' | ...
    on_fire: TriggerFn


# ─── The registries (singleton) ─────────────────────────────────────

_retrievers: Dict[str, ContextRetriever] = {}
_types:      Dict[str, SuggestionType]   = {}
_triggers:   Dict[str, List[Trigger]]    = {}  # multiple triggers per event allowed


def register_retriever(retriever: ContextRetriever) -> None:
    """Register or replace a retriever by name. Idempotent — later
    register() calls overwrite earlier ones, so addon hot-reload
    works without engine restart."""
    if retriever.name in _retrievers:
        log.info("registry: replacing retriever %r", retriever.name)
    _retrievers[retriever.name] = retriever


def register_type(stype: SuggestionType) -> None:
    if stype.type in _types:
        log.info("registry: replacing suggestion type %r", stype.type)
    _types[stype.type] = stype


def register_trigger(trigger: Trigger) -> None:
    _triggers.setdefault(trigger.event, []).append(trigger)


# Accessors — engine + UI use these. Returning copies wouldn't help
# (callers don't mutate); returning the dict directly is fine and
# faster.

def get_retrievers(scope: Optional[str] = None) -> List[ContextRetriever]:
    """All registered retrievers, optionally filtered to ones whose
    scope includes the given marker ('message' / 'contact'). Engine
    uses this to decide which retrievers to run for a given analysis."""
    out = list(_retrievers.values())
    if scope is not None:
        out = [r for r in out if scope in r.scope]
    return out


def get_type(type_name: str) -> Optional[SuggestionType]:
    return _types.get(type_name)


def known_type_names() -> List[str]:
    """Used to constrain the LLM's structured output — the JSON
    schema's `type` enum is exactly this list."""
    return sorted(_types.keys())


def get_triggers(event: str) -> List[Trigger]:
    return list(_triggers.get(event, []))


def all_types() -> List[SuggestionType]:
    return list(_types.values())


def reset_for_tests() -> None:
    """Test-only escape hatch. Production code shouldn't need this —
    the registries are populated once at import time and stay stable
    for the process lifetime."""
    _retrievers.clear()
    _types.clear()
    _triggers.clear()
