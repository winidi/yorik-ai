"""wa.new trigger — fires analyse_message for new inbound WA messages.

Called by backend.whatsapp on every 1:1 inbound message (groups
are skipped at the caller — group-chat suggestions are too noisy
and the contact resolution is ambiguous). Event payload:
{owner_user_id, message_id} where message_id is the surrogate
wa_messages.id BIGINT added in mig 124.

Schedules analysis onto the asyncio loop so the WS subscriber
thread is never blocked by the LLM call."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from ..registry import Trigger, register_trigger

log = logging.getLogger("yorik.suggestions.triggers.wa_new")


async def _on_fire(payload: Dict[str, Any]) -> None:
    owner_user_id = payload.get("owner_user_id")
    message_id = payload.get("message_id")
    if not owner_user_id or not message_id:
        log.debug("wa.new trigger missing fields: %r", payload)
        return
    from .. import engine
    try:
        result = await engine.analyse_message(
            owner_user_id=str(owner_user_id),
            source_kind="wa",
            source_id=int(message_id),
        )
        if result.get("count"):
            log.info("wa.new analyse → %d suggestions for msg %s",
                     result["count"], message_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("wa.new analyse failed for msg %s: %s", message_id, exc)


def fire_from_thread(loop: asyncio.AbstractEventLoop,
                     owner_user_id: str, message_id: int) -> None:
    """Convenience: schedule from a worker thread (the WS subscriber
    runs in one). Falls back silently if no running loop."""
    try:
        asyncio.run_coroutine_threadsafe(
            _on_fire({"owner_user_id": owner_user_id, "message_id": message_id}),
            loop,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("schedule from thread failed: %s", exc)


register_trigger(Trigger(event="wa.new", on_fire=_on_fire))
