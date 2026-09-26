"""Reminders at a time — the notification bell (and push) at `due_at`.

The chat's remind_me skill writes a row; a once-a-minute loop fires the
due ones through notifications.create, which also pushes to the
person's phone. A row fires once (fired_at), a cancelled one never.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .database import get_conn

log = logging.getLogger("yorik.reminders")


def household_tz():
    from .push import _tz
    return _tz()


def parse_when(at: Optional[str], in_minutes: Optional[int], *, now: Optional[datetime] = None) -> datetime:
    """The due moment as an aware datetime. `at` without an offset is
    household local time ("2026-10-01T07:00" = 7 Uhr in Peine)."""
    now = now or datetime.now(timezone.utc)
    if in_minutes not in (None, ""):
        minutes = int(in_minutes)
        if minutes < 1 or minutes > 60 * 24 * 366:
            raise ValueError("in_minutes must be between 1 and one year")
        return now + timedelta(minutes=minutes)
    if not at:
        raise ValueError("give either at (date and time) or in_minutes")
    try:
        due = datetime.fromisoformat(str(at).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"at={at!r} is not an ISO date-time like 2026-10-01T07:00") from exc
    if due.tzinfo is None:
        due = due.replace(tzinfo=household_tz())
    if due <= now:
        raise ValueError("that moment is already past — ask the user for a time in the future")
    return due


def create(user_id: str, title: str, due_at: datetime, body: Optional[str] = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, title, body, due_at) VALUES (?, ?, ?, ?) RETURNING id",
            (str(user_id), title, body, due_at.astimezone(timezone.utc).isoformat()),
        )
        rid = int(cur.fetchone()["id"])
        conn.commit()
    return rid


def cancel(reminder_id: int, user_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE reminders SET cancelled_at = now() WHERE id = ? AND user_id = ? "
            "AND fired_at IS NULL AND cancelled_at IS NULL", (int(reminder_id), str(user_id)))
        conn.commit()
        return (cur.rowcount or 0) > 0


def fire_due(now: Optional[datetime] = None) -> int:
    """Called once a minute: every due, open reminder becomes a bell
    entry (with push). Claimed with fired_at first, so two workers never
    fire the same row twice."""
    from . import notifications as _notif
    now = now or datetime.now(timezone.utc)
    with get_conn() as conn:
        rows: List[Dict[str, Any]] = [dict(r) for r in conn.execute(
            "UPDATE reminders SET fired_at = ? WHERE id IN ("
            "  SELECT id FROM reminders WHERE fired_at IS NULL AND cancelled_at IS NULL AND due_at <= ?"
            ") RETURNING id, user_id, title, body",
            (now.isoformat(), now.isoformat())).fetchall()]
        conn.commit()
    for r in rows:
        try:
            _notif.create(user_id=str(r["user_id"]), kind="reminder", title=r["title"],
                          body=r["body"], payload={"reminder_id": r["id"]})
        except Exception:  # noqa: BLE001
            log.exception("reminder %s failed", r["id"])
    return len(rows)


_scheduler_task: Optional[asyncio.Task] = None


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_task = loop.create_task(_loop(), name="reminders")


async def _loop() -> None:
    from . import workers
    workers.register("reminders", kind="scheduler")
    while True:
        try:
            n = await asyncio.to_thread(fire_due)
            workers.heartbeat("reminders", "ok", f"fired {n}" if n else "")
        except Exception as exc:  # noqa: BLE001
            workers.heartbeat("reminders", "warn", str(exc)[:120])
            log.exception("reminder loop")
        await asyncio.sleep(60 - datetime.now().second)
