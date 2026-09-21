"""Bring an existing calendar into Yorik.

Two ways, both one-directional (nothing ever goes from Yorik to the
other side):

  import      an .ics file (Google: Settings → Import & export →
              Export) lands in a calendar of yours. Events keep their
              UID, so importing the same file again updates instead of
              doubling.
  subscribe   a secret iCal address (Google: Settings → your calendar →
              "Secret address in iCal format") becomes a read-only
              mirror calendar, refreshed every SYNC_INTERVAL_S.

A mirror is replaced to match its source on every sync; a fetch that
fails, or that suddenly comes back empty, changes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from .database import get_conn

log = logging.getLogger("yorik.calendar_import")

SYNC_INTERVAL_S = int(os.getenv("YORIK_CALENDAR_FEED_INTERVAL_S", "900"))
MAX_ICS_BYTES = 15 * 1024 * 1024
FIELDS = ("title", "starts_at", "ends_at", "all_day", "location", "notes", "recurring")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _own_calendar(calendar_id: int, user_id: str) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM calendars WHERE id = ? AND owner_user_id = ? AND archived_at IS NULL",
                           (int(calendar_id), user_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="no such calendar of yours")
    return dict(row)


def apply_events(calendar_id: int, owner_user_id: str, rows: List[Dict[str, Any]], *, mirror: bool) -> Dict[str, int]:
    """Upsert parsed events into a calendar by ical_uid. `mirror=True`
    also removes events of this calendar the source no longer has."""
    stats = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0}
    with get_conn() as conn:
        cal = conn.execute("SELECT space_id FROM calendars WHERE id = ?", (calendar_id,)).fetchone()
        space_id = cal["space_id"] if cal else None
        existing = {r["ical_uid"]: dict(r) for r in conn.execute(
            "SELECT id, ical_uid, title, starts_at, ends_at, all_day, location, notes, recurring "
            "FROM events WHERE calendar_id = ? AND ical_uid IS NOT NULL", (calendar_id,)).fetchall()}
        seen = set()
        for ev in rows:
            uid = ev["ical_uid"]
            if uid in seen:
                continue
            seen.add(uid)
            values = {k: (int(bool(ev[k])) if k == "all_day" else ev.get(k)) for k in FIELDS}
            old = existing.get(uid)
            if old is None:
                conn.execute(
                    "INSERT INTO events (title, starts_at, ends_at, all_day, location, notes, recurring, "
                    "calendar_id, owner_user_id, space_id, ical_uid, visibility) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'default')",
                    (*[values[k] for k in FIELDS], calendar_id, owner_user_id, space_id, uid))
                stats["added"] += 1
            elif any((old[k] or None) != (values[k] or None) for k in FIELDS):
                conn.execute(
                    "UPDATE events SET title = ?, starts_at = ?, ends_at = ?, all_day = ?, location = ?, notes = ?, "
                    "recurring = ? WHERE id = ?", (*[values[k] for k in FIELDS], old["id"]))
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1
        if mirror:
            for uid, old in existing.items():
                if uid not in seen:
                    conn.execute("DELETE FROM events WHERE id = ?", (old["id"],))
                    stats["removed"] += 1
        conn.commit()
    return stats


# ─── subscribed calendars ────────────────────────────────────────────

def _encrypt(url: str) -> str:
    from .credential_store import _get_fernet
    return _get_fernet().encrypt(url.encode()).decode()


def _decrypt(token: str) -> str:
    from .credential_store import _get_fernet
    return _get_fernet().decrypt(token.encode()).decode()


def _check_url(url: str) -> str:
    """https only, and never a host inside the house (the server fetches
    this address on a schedule)."""
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="the address must start with https:// (or webcal://)")
    from .agent.providers.web_search.trafilatura import _is_private_or_local
    if _is_private_or_local(parsed.hostname):
        raise HTTPException(status_code=400, detail="addresses inside the local network are not fetched")
    return url


def fetch_ics(url: str, etag: Optional[str] = None) -> tuple[Optional[bytes], Optional[str]]:
    """(body, etag); body None = not modified. Module-level so tests swap it."""
    import requests
    headers = {"User-Agent": "yorik-calendar-feed", "Accept": "text/calendar, */*;q=0.5"}
    if etag:
        headers["If-None-Match"] = etag
    r = requests.get(url, headers=headers, timeout=30, stream=True, allow_redirects=True)
    if r.status_code == 304:
        return None, etag
    r.raise_for_status()
    body = b""
    for chunk in r.iter_content(64 * 1024):
        body += chunk
        if len(body) > MAX_ICS_BYTES:
            raise ValueError("the calendar is larger than 15 MB")
    return body, r.headers.get("ETag")


def sync_feed(feed_id: int) -> Dict[str, Any]:
    from . import email_invites
    with get_conn() as conn:
        feed = conn.execute("SELECT * FROM calendar_feeds WHERE id = ?", (int(feed_id),)).fetchone()
    if not feed:
        return {"ok": False, "error": "no such feed"}
    feed = dict(feed)
    try:
        body, etag = fetch_ics(_decrypt(feed["url_enc"]), feed.get("etag"))
        if body is None:
            stats = {"added": 0, "updated": 0, "unchanged": feed["event_count"], "removed": 0}
        else:
            if b"BEGIN:VCALENDAR" not in body[:2000].upper():
                raise ValueError("the address did not return a calendar (wrong or revoked link?)")
            rows = email_invites.parse_calendar(body)
            # A source that suddenly has nothing is far more often a broken
            # fetch than an emptied calendar: keep what is there.
            if not rows and feed["event_count"] > 0:
                raise ValueError("the calendar came back empty; kept the last state")
            stats = apply_events(feed["calendar_id"], str(feed["owner_user_id"]), rows, mirror=True)
            stats["total"] = len(rows)
        with get_conn() as conn:
            conn.execute("UPDATE calendar_feeds SET last_sync_at = ?, last_status = 'ok', last_error = NULL, etag = ?, "
                         "event_count = COALESCE(?, event_count) WHERE id = ?",
                         (_now(), etag, stats.get("total"), feed["id"]))
            conn.commit()
        return {"ok": True, **stats}
    except Exception as exc:  # noqa: BLE001 — a failed sync is a status, not a crash
        message = exc.detail if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
        with get_conn() as conn:
            conn.execute("UPDATE calendar_feeds SET last_sync_at = ?, last_status = 'error', last_error = ? WHERE id = ?",
                         (_now(), str(message)[:300], feed["id"]))
            conn.commit()
        return {"ok": False, "error": str(message)}


def sync_all() -> Dict[str, int]:
    with get_conn() as conn:
        ids = [int(r["id"]) for r in conn.execute("SELECT id FROM calendar_feeds ORDER BY id").fetchall()]
    ok = sum(1 for i in ids if sync_feed(i).get("ok"))
    return {"feeds": len(ids), "ok": ok}


_task = None


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    from . import workers
    global _task
    workers.register("calendar-feeds", kind="sync", expected_interval_s=SYNC_INTERVAL_S)

    async def _loop():
        await asyncio.sleep(60)
        while True:
            try:
                r = await asyncio.get_running_loop().run_in_executor(None, sync_all)
                workers.heartbeat("calendar-feeds", "ok" if r["ok"] == r["feeds"] else "warn",
                                  f"{r['ok']}/{r['feeds']} subscribed calendars in sync")
            except Exception as exc:  # noqa: BLE001
                log.warning("calendar feeds: sweep failed: %s", exc)
            await asyncio.sleep(SYNC_INTERVAL_S)

    _task = loop.create_task(_loop(), name="calendar-feeds")


# ─── routes ──────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/calendar-import", tags=["calendar"])


def _current_user():
    from .auth_sessions import current_user
    return current_user


def _public_feed(f: Dict[str, Any]) -> Dict[str, Any]:
    return {k: f[k] for k in ("id", "calendar_id", "url_host", "last_sync_at", "last_status", "last_error", "event_count")}


@router.post("/file")
async def import_file(file: UploadFile = File(...), calendar_id: int = Query(...), dry_run: bool = Query(False),
                      user: Dict[str, Any] = Depends(_current_user())) -> Dict[str, Any]:
    """Import an .ics file into one of your calendars. dry_run=1 only counts."""
    from . import email_invites
    cal = _own_calendar(calendar_id, str(user["id"]))
    if cal.get("read_only"):
        raise HTTPException(status_code=400, detail="this calendar mirrors another one; import into a calendar of your own")
    data = await file.read(MAX_ICS_BYTES + 1)
    if len(data) > MAX_ICS_BYTES:
        raise HTTPException(status_code=413, detail="the file is larger than 15 MB")
    if b"BEGIN:VCALENDAR" not in data[:2000].upper():
        raise HTTPException(status_code=400, detail="this is not an .ics calendar file")
    rows = await asyncio.to_thread(email_invites.parse_calendar, data)
    summary = {"events": len(rows), "series": sum(1 for r in rows if r["recurring"]),
               "first": min((r["starts_at"] for r in rows), default=None),
               "last": max((r["starts_at"] for r in rows), default=None)}
    if dry_run:
        return {"ok": True, "dry_run": True, **summary}
    stats = await asyncio.to_thread(apply_events, calendar_id, str(user["id"]), rows, mirror=False)
    return {"ok": True, **summary, **stats}


class FeedIn(BaseModel):
    url: str
    name: str
    color: Optional[str] = None


@router.get("/feeds")
def list_feeds(user: Dict[str, Any] = Depends(_current_user())) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM calendar_feeds WHERE owner_user_id = ? ORDER BY id", (str(user["id"]),)).fetchall()
    return [_public_feed(dict(r)) for r in rows]


@router.post("/feeds", status_code=201)
async def subscribe(body: FeedIn, user: Dict[str, Any] = Depends(_current_user())) -> Dict[str, Any]:
    if user.get("auth") == "api_token":
        raise HTTPException(status_code=403, detail="log in to subscribe to a calendar")
    url = _check_url(body.url)
    from . import calendars as _cal
    calendar_id = _cal.create_calendar(name=(body.name or "").strip() or "Google", owner_user_id=str(user["id"]),
                                       color=body.color or "#4285f4", kind="personal")
    with get_conn() as conn:
        conn.execute("UPDATE calendars SET read_only = 1 WHERE id = ?", (calendar_id,))
        feed_id = conn.execute(
            "INSERT INTO calendar_feeds (owner_user_id, calendar_id, url_enc, url_host, created_at) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (str(user["id"]), calendar_id, _encrypt(url), urlparse(url).hostname, _now())).fetchone()["id"]
        conn.commit()
    result = await asyncio.to_thread(sync_feed, feed_id)
    if not result.get("ok"):
        # a wrong address should not leave an empty calendar behind
        _remove_feed(feed_id, str(user["id"]))
        raise HTTPException(status_code=400, detail=f"could not read this calendar: {result.get('error')}")
    with get_conn() as conn:
        feed = dict(conn.execute("SELECT * FROM calendar_feeds WHERE id = ?", (feed_id,)).fetchone())
    return {**_public_feed(feed), "sync": result}


@router.post("/feeds/{feed_id}/sync")
async def sync_now(feed_id: int, user: Dict[str, Any] = Depends(_current_user())) -> Dict[str, Any]:
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM calendar_feeds WHERE id = ? AND owner_user_id = ?",
                            (feed_id, str(user["id"]))).fetchone():
            raise HTTPException(status_code=404, detail="no such subscription")
    return await asyncio.to_thread(sync_feed, feed_id)


def _remove_feed(feed_id: int, user_id: str) -> bool:
    with get_conn() as conn:
        feed = conn.execute("SELECT calendar_id FROM calendar_feeds WHERE id = ? AND owner_user_id = ?",
                            (feed_id, user_id)).fetchone()
        if not feed:
            return False
        conn.execute("DELETE FROM events WHERE calendar_id = ?", (feed["calendar_id"],))
        conn.execute("DELETE FROM calendars WHERE id = ? AND read_only = 1", (feed["calendar_id"],))
        conn.execute("DELETE FROM calendar_feeds WHERE id = ?", (feed_id,))
        conn.commit()
    return True


@router.delete("/feeds/{feed_id}", status_code=204)
def unsubscribe(feed_id: int, user: Dict[str, Any] = Depends(_current_user())):
    """End a subscription; the mirror calendar and its events go with it."""
    if not _remove_feed(feed_id, str(user["id"])):
        raise HTTPException(status_code=404, detail="no such subscription")
