"""The outbox: what was queued leaves the house, with retries, and every
payload is written to the log first.

Only rows with status 'queued' are sent — an error report gets there
through the person's review (report.queue), a daily counts row through
usage.collect_daily. The collector address comes from
diagnostics.collector_url(); empty means reports stay here. Failures
back off (5 min × 2^n) and park after 8 attempts. Consent is checked on
every tick, not at start, so a switch needs no restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.database import get_conn

from . import any_enabled, collector_url, enabled, install_id

log = logging.getLogger("yorik.diagnostics")

TICK_S = 900
MAX_ATTEMPTS = 8
TIMEOUT_S = 10.0
_wake: Optional[asyncio.Event] = None
_task: Optional[asyncio.Task] = None


def wake() -> None:
    if _wake is not None:
        try:
            _wake.set()
        except Exception:  # noqa: BLE001
            pass


def _post(url: str, body: Dict[str, Any], headers: Dict[str, str]) -> int:
    """One HTTP POST; returns the status code. Separate so tests can
    replace it and so no other module is tempted to send."""
    import httpx
    with httpx.Client(timeout=TIMEOUT_S) as client:
        r = client.post(url, json=body, headers=headers)
    return r.status_code


def flush(now: Optional[datetime] = None) -> Dict[str, int]:
    """Send what is due. Returns counts: sent, failed, skipped."""
    out = {"sent": 0, "failed": 0, "skipped": 0}
    if not any_enabled():
        return out
    url = collector_url()
    now = now or datetime.now(timezone.utc)
    with get_conn() as conn:
        rows = conn.execute("SELECT id, kind, payload, delete_token, attempts FROM diag_reports WHERE status = 'queued' "
                            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY created_at LIMIT 20",
                            (now.isoformat(),)).fetchall()
    for r in rows:
        rid = str(r["id"])
        tier = 3 if r["kind"] == "error" else 1
        if not enabled(tier):
            out["skipped"] += 1
            continue
        payload = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if not url:
            with get_conn() as conn:
                conn.execute("UPDATE diag_reports SET last_error = 'no collector configured — kept here', next_attempt_at = ? WHERE id = ?",
                             ((now + timedelta(days=1)).isoformat(), rid))
                conn.commit()
            out["skipped"] += 1
            continue
        body = {"report": payload, "delete_token_sha256": hashlib.sha256(str(r["delete_token"] or "").encode()).hexdigest()}
        log.info("diagnostics: sending %s (%d bytes) to %s: %s", rid, len(text), url, text)
        try:
            status = _post(f"{url}/reports", body, {"X-Yorik-Install": install_id(), "Content-Type": "application/json"})
            ok = 200 <= status < 300
            err = None if ok else f"collector answered {status}"
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"{type(exc).__name__}: {str(exc)[:120]}"
        attempts = int(r["attempts"] or 0) + 1
        with get_conn() as conn:
            if ok:
                conn.execute("UPDATE diag_reports SET status = 'sent', sent_at = ?, attempts = ?, last_error = NULL WHERE id = ?",
                             (now.isoformat(), attempts, rid))
                out["sent"] += 1
            else:
                parked = attempts >= MAX_ATTEMPTS
                conn.execute("UPDATE diag_reports SET status = ?, attempts = ?, last_error = ?, next_attempt_at = ? WHERE id = ?",
                             ("failed" if parked else "queued", attempts, err,
                              (now + timedelta(minutes=5 * (2 ** min(attempts, 6)))).isoformat(), rid))
                out["failed"] += 1
            conn.commit()
    return out


def request_deletions() -> int:
    """Ask the collector to forget every report this installation sent
    (consent withdrawn, identity reset). Best effort; returns how many
    requests were made."""
    url = collector_url()
    if not url:
        return 0
    with get_conn() as conn:
        rows = conn.execute("SELECT id, delete_token FROM diag_reports WHERE status = 'sent' AND delete_token IS NOT NULL").fetchall()
    n = 0
    for r in rows:
        try:
            _post(f"{url}/reports/{r['id']}/delete", {"delete_token": r["delete_token"]}, {"X-Yorik-Install": install_id()})
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.info("diagnostics: deletion request for %s failed: %s", r["id"], exc)
    return n


def prune(now: Optional[datetime] = None) -> int:
    """Drafts nobody acted on and sent rows older than 90 days go."""
    now = now or datetime.now(timezone.utc)
    with get_conn() as conn:
        n = conn.execute("DELETE FROM diag_reports WHERE (status = 'draft' AND created_at < ?) OR (status IN ('sent','declined','failed') AND created_at < ?)",
                         ((now - timedelta(days=7)).isoformat(), (now - timedelta(days=90)).isoformat())).rowcount or 0
        conn.commit()
    return n


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    """Every TICK_S, or when woken: the daily counts (usage), then the
    queue. Same shape as search_index.start_scheduler."""
    global _wake, _task
    if _task and not _task.done():
        return
    from backend import workers
    _wake = asyncio.Event()
    workers.register("diagnostics-outbox", kind="outbox", expected_interval_s=TICK_S)

    async def _loop() -> None:
        await asyncio.sleep(60)
        while True:
            try:
                if any_enabled():
                    try:
                        from . import usage
                        await asyncio.get_running_loop().run_in_executor(None, usage.collect_daily)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("diagnostics: daily counts failed: %s", exc)
                    result = await asyncio.get_running_loop().run_in_executor(None, flush)
                    await asyncio.get_running_loop().run_in_executor(None, prune)
                    workers.heartbeat("diagnostics-outbox", "ok", f"sent {result['sent']}, failed {result['failed']}")
                else:
                    workers.heartbeat("diagnostics-outbox", "ok", "off (no consent)")
            except Exception as exc:  # noqa: BLE001
                workers.heartbeat("diagnostics-outbox", "warn", str(exc)[:120])
                log.warning("diagnostics outbox: %s", exc)
            try:
                await asyncio.wait_for(_wake.wait(), timeout=TICK_S)
            except asyncio.TimeoutError:
                pass
            _wake.clear()

    _task = loop.create_task(_loop(), name="diagnostics-outbox")
