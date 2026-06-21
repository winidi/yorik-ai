"""email.new trigger — fires analyse_message for new inbound emails.

Called by backend.email_fetcher right after the autodraft schedule.
Event payload: {owner_user_id, message_id}. The trigger schedules
engine.analyse_message onto the main asyncio loop so the IMAP worker
thread is never blocked by the LLM call."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from ..registry import Trigger, register_trigger

log = logging.getLogger("yorik.suggestions.triggers.email_new")


async def _on_fire(payload: Dict[str, Any]) -> None:
    owner_user_id = payload.get("owner_user_id")
    message_id = payload.get("message_id")
    if not owner_user_id or not message_id:
        log.debug("email.new trigger missing fields: %r", payload)
        return
    from .. import engine
    try:
        result = await engine.analyse_message(
            owner_user_id=str(owner_user_id),
            source_kind="email",
            source_id=int(message_id),
        )
        if result.get("count"):
            log.info("email.new analyse → %d suggestions for msg %s",
                     result["count"], message_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("email.new analyse failed for msg %s: %s", message_id, exc)


def fire_from_thread(loop: asyncio.AbstractEventLoop,
                     owner_user_id: str, message_id: int) -> None:
    """Convenience: schedule the trigger from a worker thread (IMAP
    fetcher runs in one). Falls back silently if no running loop."""
    try:
        asyncio.run_coroutine_threadsafe(
            _on_fire({"owner_user_id": owner_user_id, "message_id": message_id}),
            loop,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("schedule from thread failed: %s", exc)


register_trigger(Trigger(event="email.new", on_fire=_on_fire))
