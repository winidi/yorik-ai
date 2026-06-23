"""Yorik 1.0 suggestion engine.

Analyses incoming messages in the context of everything Yorik knows
about the sender, emits typed suggestions (draft_reply, propose_
meeting_slot, ...) with evidence-backed reasoning.

Architecture is plugin-first: yorik-core itself ships as the first
"plugin" — every retriever, suggestion type, and trigger registers
through the same contract that a future third-party addon would use.
The dispatch path doesn't know which is which.

See backend/suggestions/registry.py for the contract.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

log = logging.getLogger("yorik.suggestions")

# The main asyncio event loop, captured at FastAPI startup. Triggers
# fired from worker threads (IMAP fetcher, WhatsApp WS subscriber,
# etc.) need an explicit loop reference for
# asyncio.run_coroutine_threadsafe — calling asyncio.get_event_loop()
# from a worker thread raises RuntimeError on Python 3.10+, which the
# old hook caught and silently turned into a "skip". The 116 inbound
# emails that arrived after the engine shipped never triggered an
# analyse_message because of exactly that swallow.
#
# Set once via set_main_loop() during the @app.on_event("startup")
# hook in main.py. Read via get_main_loop() from any thread.
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Stash the running loop. Called once at FastAPI startup."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop
    log.info("suggestions: main loop captured for cross-thread trigger fires")


def get_main_loop() -> Optional[asyncio.AbstractEventLoop]:
    """Return the captured main loop, or None if startup hasn't run yet
    or the loop is no longer alive. Callers should treat None as
    'skip the trigger' — never crash on it."""
    loop = _MAIN_LOOP
    if loop is None or loop.is_closed():
        return None
    return loop
