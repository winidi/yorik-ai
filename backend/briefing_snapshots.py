"""Per-day briefing snapshots — the "time machine" half of /r/briefing.

When the day ends, the snapshot scheduler runs the configured "recap"
template (`day-recap`) for yesterday and stashes the full result in
`briefing_snapshots`. The frontend's day navigator can then walk
backwards to any previously-captured date and see what was happening
at the time, even after the underlying email / WhatsApp / photo data
has drifted (new categories assigned, messages deleted, etc.).

A snapshot is one person's day (their mail, calendar, WhatsApp), so
there is one per (template, date, person). Until 2026-09-22 there was
one per (template, date), taken as the first admin and served to
everyone who asked for a past date (audit 2.8).

Read path (used by briefing_routes):
    get_snapshot(template_id, target_date, user_id) → dict | None

Write path (called by the scheduler or an explicit /api/briefings/snapshot
POST):
    await capture_snapshot(template_id, target_date, user_id, role)

Scheduler:
    start_scheduler(loop) starts a daily fire at 03:00 local time that
    captures yesterday's `day-recap` for every enabled person. Pattern
    lifted from backup.start_scheduler — wake every 60s, compare HH:MM,
    track last-triggered-minute to avoid double-firing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .database import DEFAULT_DB_PATH, conn_ctx

log = logging.getLogger("yorik.briefing_snapshots")

DB_PATH = os.getenv("HOMEOS_DB_PATH", DEFAULT_DB_PATH)

# Default to capturing day-recap nightly. Future: read from a settings
# table so the user can pick a different template or fire time.
SNAPSHOT_TEMPLATE_ID = "day-recap"
SNAPSHOT_FIRE_TIME = "03:00"


def get_snapshot(template_id: str, target_date: str, user_id: Any) -> Optional[dict[str, Any]]:
    """Return the person's saved briefing payload for that date, or
    None. `target_date` is YYYY-MM-DD."""
    if user_id is None:
        return None
    with conn_ctx(DB_PATH) as conn:
        row = conn.execute(
            "SELECT payload_json, generated_at FROM briefing_snapshots "
            "WHERE template_id = ? AND target_date = ? AND user_id = ?",
            (template_id, target_date, user_id),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        return None
    payload["_snapshot"] = {"generated_at": row["generated_at"]}
    return payload


def list_snapshot_dates(user_id: Any, template_id: str = SNAPSHOT_TEMPLATE_ID, limit: int = 60) -> list[str]:
    """Recent dates the person has a saved snapshot for. Powers the
    date navigator — only let them click back to dates that actually
    have content. Newest first."""
    if user_id is None:
        return []
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT target_date FROM briefing_snapshots "
            "WHERE template_id = ? AND user_id = ? "
            "ORDER BY target_date DESC LIMIT ?",
            (template_id, user_id, limit),
        ).fetchall()
    return [r["target_date"] for r in rows]


async def capture_snapshot(template_id: str, target_date: str,
                            user_id: str, role: str = "admin") -> dict[str, Any]:
    """Run the briefing FOR `target_date` as `user_id` and persist the
    result as that person's. Idempotent: re-running for the same
    (template, date, person) overwrites."""
    from . import briefings
    payload = await briefings.run_briefing(
        template_id=template_id,
        user_id=user_id,
        role=role,
        for_date=target_date,
    )
    if "error" in payload and "template" not in payload:
        log.warning("capture_snapshot: briefing run failed for %s @ %s: %s",
                    template_id, target_date, payload.get("error"))
        return {"ok": False, "error": payload.get("error")}

    with conn_ctx(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO briefing_snapshots "
            "(template_id, target_date, user_id, payload_json, generated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (template_id, target_date, user_id) DO UPDATE SET "
            "payload_json = EXCLUDED.payload_json, generated_at = EXCLUDED.generated_at",
            (
                template_id, target_date, user_id,
                json.dumps(payload, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    log.info("snapshot saved: %s @ %s for user %s", template_id, target_date, user_id)
    return {"ok": True, "template_id": template_id, "target_date": target_date}


# ─────────────────────── scheduler ──────────────────────────────────

_scheduler_task: Optional[asyncio.Task] = None
_scheduler_stop: Optional[asyncio.Event] = None
_last_fired_minute: Optional[str] = None


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    """Start the nightly snapshot job. Fires at SNAPSHOT_FIRE_TIME local
    time, captures yesterday's day-recap. Cheap to leave running — wake
    every 60s, compare HH:MM."""
    global _scheduler_task, _scheduler_stop
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_stop = asyncio.Event()
    _scheduler_task = loop.create_task(_scheduler_loop(), name="briefing-snapshot-scheduler")


async def stop_scheduler() -> None:
    global _scheduler_task, _scheduler_stop
    if _scheduler_stop:
        _scheduler_stop.set()
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass


def _people() -> list[tuple[Any, str]]:
    """Every enabled person as (user_id, role): each gets their own
    snapshot of their own day."""
    with conn_ctx(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, role FROM user_profiles WHERE (disabled = 0 OR disabled IS NULL) ORDER BY created_at"
        ).fetchall()
    return [(r["id"], r["role"]) for r in rows]


async def _scheduler_loop() -> None:
    global _last_fired_minute
    from . import workers
    workers.register("briefing_snapshot", kind="scheduler")
    try:
        while True:
            if _scheduler_stop and _scheduler_stop.is_set():
                workers.heartbeat("briefing_snapshot", "warn", "stopped")
                return
            now = datetime.now()
            hhmm = now.strftime("%H:%M")
            if hhmm == SNAPSHOT_FIRE_TIME and hhmm != _last_fired_minute:
                _last_fired_minute = hhmm
                yesterday_iso = (date.today() - timedelta(days=1)).isoformat()
                people = _people()
                if not people:
                    workers.heartbeat("briefing_snapshot", "warn",
                                      "no users — can't snapshot")
                else:
                    done = 0
                    for user_id, role in people:
                        try:
                            await capture_snapshot(
                                SNAPSHOT_TEMPLATE_ID, yesterday_iso,
                                user_id=user_id, role=role,
                            )
                            done += 1
                        except Exception as e:
                            log.exception("snapshot for user %s failed: %s", user_id, e)
                    workers.heartbeat("briefing_snapshot", "ok" if done else "warn",
                                      f"snapshotted {yesterday_iso} for {done}/{len(people)} people")
            else:
                workers.heartbeat("briefing_snapshot", "ok",
                                  f"armed for {SNAPSHOT_FIRE_TIME}")
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        workers.report_error("briefing_snapshot", "cancelled")
        return
