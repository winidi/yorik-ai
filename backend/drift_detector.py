"""Phase B.6 — periodic provisioning reconciliation worker.

Spaces define the authoritative ACL; Paperless groups and Immich albums
mirror it. The provisioning hooks (spaces.on_space_member_added/
_removed) keep the mirror in sync on every membership change, BUT they
miss two real-world drift sources:

  1. Direct edits in Paperless/Immich UI ("admin removed someone from
     the household group from inside Paperless").
  2. Bundled-service restarts that lose state, or partial provisioning
     failures the hook swallowed.

This worker fires every DRIFT_INTERVAL_S seconds (default 1h), calls
sync_all_shared_spaces() on both connectors, and logs any add/remove
deltas to yorik.drift. Drift > 0 isn't an error — it's the worker
doing what it exists for. Drift = 0 every tick means everything is in
lockstep.

Starts in main.py:_startup(). Cancellable cleanly via the global
shutdown handler.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

log = logging.getLogger("yorik.drift")

# Default 1h. Override via env for tests / faster local feedback.
DRIFT_INTERVAL_S = int(os.environ.get("YORIK_DRIFT_INTERVAL_S", "3600"))


_task: Optional[asyncio.Task] = None


def start_worker() -> None:
    """Schedule the drift worker on the running asyncio loop. Idempotent
    — re-calling won't spawn duplicates. Safe to call from a sync
    startup hook."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        log.warning("drift worker: no running event loop; will not start")
        return
    _task = loop.create_task(_run_forever(), name="yorik-drift-detector")
    log.info("drift worker scheduled (interval=%ds)", DRIFT_INTERVAL_S)


def stop_worker() -> None:
    """Cancel the worker. Called from app shutdown."""
    global _task
    if _task is None or _task.done():
        return
    _task.cancel()
    _task = None


async def _run_forever() -> None:
    # First tick after a short delay so startup logs aren't drowned by
    # a sync burst. After that, every interval.
    await asyncio.sleep(min(60, DRIFT_INTERVAL_S))
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("drift tick crashed: %s", exc)
        await asyncio.sleep(DRIFT_INTERVAL_S)


async def _tick() -> None:
    """One reconcile pass. Each connector's sync_all_shared_spaces is
    sync code (uses requests), so we run them in a thread to keep the
    event loop free."""
    loop = asyncio.get_event_loop()
    summaries = await loop.run_in_executor(None, _run_both_connectors)
    _log_drift(summaries)


def _run_both_connectors() -> dict[str, list]:
    """Synchronous fanout for both provisioning modules. Errors per
    connector are logged + swallowed so one outage doesn't suppress
    the other."""
    out: dict[str, list] = {"paperless": [], "immich": []}
    try:
        from . import paperless_provisioning as _pp
        out["paperless"] = _pp.sync_all_shared_spaces()
    except Exception as exc:  # noqa: BLE001
        log.warning("paperless drift sync failed: %s", exc)
    try:
        from . import immich_provisioning as _ip
        out["immich"] = _ip.sync_all_shared_spaces()
    except Exception as exc:  # noqa: BLE001
        log.warning("immich drift sync failed: %s", exc)
    return out


def _log_drift(summaries: dict[str, list]) -> None:
    """Emit a one-liner per connector + spotlight non-empty add/remove
    sets at INFO level. Quiet ticks log at DEBUG so noise stays low."""
    for connector, results in summaries.items():
        total_added = sum(len(r.get("added") or []) for r in results)
        total_removed = sum(len(r.get("removed") or []) for r in results)
        total_skipped = sum(len(r.get("skipped_users") or []) for r in results)
        if total_added or total_removed:
            log.info(
                "drift %s: added=%d removed=%d skipped=%d spaces=%d",
                connector, total_added, total_removed, total_skipped, len(results),
            )
            for r in results:
                if r.get("added") or r.get("removed"):
                    log.info("  space=%s added=%s removed=%s",
                             r.get("space_id"), r.get("added"), r.get("removed"))
        else:
            log.debug(
                "drift %s: clean (spaces=%d skipped=%d)",
                connector, len(results), total_skipped,
            )
