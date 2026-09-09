"""Web Push — Yorik taps the user on the shoulder.

This is what makes Yorik's own app a full replacement for a messenger
channel: the bell already collects what happened, Web Push carries it
to the phone while the app is closed. Every in-app notification is
pushed to all of the user's subscribed devices; two optional daily
nudges (morning "plan your day", evening "how did it go") open the chat
with the right question.

Keys: a VAPID key pair is generated once into HOMEOS_VAPID_FILE
(default data/.vapid.json). Subscriptions come from the browser
(PushManager.subscribe) and are stored per device. A subscription that
answers 404/410 is deleted; other failures are counted and the row is
dropped after five in a row.

Runs entirely on this box: the push service is the browser vendor's
relay (Mozilla, Google, Apple), which sees only an encrypted blob.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .database import get_conn

log = logging.getLogger("yorik.push")

router = APIRouter(prefix="/api/push", tags=["push"])

VAPID_FILE = Path(os.getenv("HOMEOS_VAPID_FILE", "data/.vapid.json"))
_MAX_FAILURES = 5
_keys: Optional[Dict[str, str]] = None


# ─── keys ───────────────────────────────────────────────────────────

def _pem_path() -> Path:
    return VAPID_FILE.with_suffix(".pem")


def keys() -> Dict[str, str]:
    """{'public': <urlsafe b64>, 'private': <PEM>} — generated on first use.
    The PEM is also kept as a file next to it, because pywebpush reads a
    private key from a path or a base64 blob, not from PEM text."""
    global _keys
    if _keys:
        return _keys
    if VAPID_FILE.exists():
        _keys = json.loads(VAPID_FILE.read_text())
        if not _pem_path().exists():
            _pem_path().write_text(_keys["private"])
            try:
                _pem_path().chmod(0o600)
            except OSError:
                pass
        return _keys
    from py_vapid import Vapid, b64urlencode
    from cryptography.hazmat.primitives import serialization
    v = Vapid()
    v.generate_keys()
    pub = v.public_key.public_bytes(serialization.Encoding.X962,
                                    serialization.PublicFormat.UncompressedPoint)
    priv_pem = v.private_key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()).decode()
    _keys = {"public": b64urlencode(pub), "private": priv_pem}
    VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
    VAPID_FILE.write_text(json.dumps(_keys))
    _pem_path().write_text(priv_pem)
    for f in (VAPID_FILE, _pem_path()):
        try:
            f.chmod(0o600)
        except OSError:
            pass
    log.info("VAPID key pair generated at %s", VAPID_FILE)
    return _keys


def _claims_email() -> str:
    return os.getenv("HOMEOS_VAPID_CONTACT") or "mailto:admin@yorik.local"


# ─── subscriptions ──────────────────────────────────────────────────

def subscribe(user_id: str, sub: Dict[str, Any], user_agent: str = "") -> int:
    endpoint = str(sub.get("endpoint") or "").strip()
    k = sub.get("keys") or {}
    if not endpoint or not k.get("p256dh") or not k.get("auth"):
        raise ValueError("subscription needs endpoint and keys.p256dh / keys.auth")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (endpoint) DO UPDATE SET user_id = EXCLUDED.user_id, p256dh = EXCLUDED.p256dh, "
            "auth = EXCLUDED.auth, user_agent = EXCLUDED.user_agent, failures = 0 RETURNING id",
            (user_id, endpoint, k["p256dh"], k["auth"], (user_agent or "")[:200]),
        )
        row = cur.fetchone()
        conn.commit()
    return int(row["id"])


def unsubscribe(user_id: str, endpoint: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
                           (user_id, endpoint))
        conn.commit()
        return cur.rowcount > 0


def subscriptions_for(user_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, endpoint, p256dh, auth, user_agent, created_at, last_ok_at, failures "
            "FROM push_subscriptions WHERE user_id = ?", (user_id,),
        ).fetchall()]


# ─── sending ────────────────────────────────────────────────────────

def send(user_id: str, *, title: str, body: str = "", url: str = "/r/home",
         tag: Optional[str] = None) -> int:
    """Push one message to every device of the user. Returns the number
    of deliveries the push service accepted. Never raises."""
    subs = subscriptions_for(user_id)
    if not subs:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("pywebpush missing — push disabled")
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or "yorik"})
    k = keys()
    ok = 0
    for s in subs:
        try:
            webpush(
                subscription_info={"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                data=payload,
                vapid_private_key=str(_pem_path()),
                vapid_claims={"sub": _claims_email()},
                ttl=6 * 3600,
                timeout=10,
            )
            ok += 1
            _mark(s["id"], success=True)
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                _drop(s["id"])
                log.info("push subscription #%s gone (%s), removed", s["id"], status)
            else:
                _mark(s["id"], success=False)
                log.warning("push to #%s failed: %s", s["id"], str(exc)[:200])
        except Exception as exc:  # noqa: BLE001
            _mark(s["id"], success=False)
            log.warning("push to #%s failed: %s", s["id"], str(exc)[:200])
    return ok


def _mark(sub_id: int, *, success: bool) -> None:
    with get_conn() as conn:
        if success:
            conn.execute("UPDATE push_subscriptions SET last_ok_at = ?, failures = 0 WHERE id = ?",
                         (datetime.now().isoformat(timespec="seconds"), sub_id))
        else:
            conn.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE id = ?", (sub_id,))
            conn.execute("DELETE FROM push_subscriptions WHERE id = ? AND failures >= ?", (sub_id, _MAX_FAILURES))
        conn.commit()


def _drop(sub_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))
        conn.commit()


# ─── daily nudges ───────────────────────────────────────────────────

MORNING_URL = "/r/chat?say=" + "Plan%20meinen%20Tag."
EVENING_URL = "/r/chat?say=" + "Wie%20war%20mein%20Tag%3F"

_last_fired: Dict[str, str] = {}   # f"{user_id}:{kind}" -> "YYYY-MM-DD HH:MM"


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("YORIK_TZ") or os.getenv("TZ") or "Europe/Berlin")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def fire_due_nudges(now: Optional[datetime] = None) -> int:
    """Called once a minute. For every user whose morning/evening time is
    now (to the minute), write a bell notification (which pushes)."""
    from . import notifications as _notif
    now = now or datetime.now(_tz())
    hhmm = now.strftime("%H:%M")
    stamp = now.strftime("%Y-%m-%d %H:%M")
    fired = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, language, nudge_morning, nudge_evening FROM user_profiles "
            "WHERE disabled = 0 AND (nudge_morning = ? OR nudge_evening = ?)", (hhmm, hhmm),
        ).fetchall()
    for r in rows:
        de = (r["language"] or "").lower().startswith("de")
        for kind, col in (("morning", "nudge_morning"), ("evening", "nudge_evening")):
            if r[col] != hhmm:
                continue
            key = f"{r['id']}:{kind}"
            if _last_fired.get(key) == stamp:
                continue
            _last_fired[key] = stamp
            if kind == "morning":
                title = "Guten Morgen. Soll ich deinen Tag planen?" if de else "Good morning. Shall I plan your day?"
                body = "Tippen, und wir gehen den Tag zusammen durch." if de else "Tap and we go through the day together."
                url = MORNING_URL
            else:
                title = "Wie war dein Tag?" if de else "How did your day go?"
                body = "Kurzer Rückblick, was du geschafft hast." if de else "A short look at what you got done."
                url = EVENING_URL
            try:
                _notif.create(user_id=str(r["id"]), kind=f"nudge_{kind}", title=title, body=body,
                              navigate_to=url)
                fired += 1
            except Exception:  # noqa: BLE001
                log.exception("nudge failed for user %s", r["id"])
    return fired


_scheduler_task: Optional[asyncio.Task] = None


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        return
    _scheduler_task = loop.create_task(_loop(), name="push-nudges")


async def _loop() -> None:
    from . import workers
    workers.register("push_nudges", kind="scheduler")
    while True:
        try:
            n = await asyncio.to_thread(fire_due_nudges)
            workers.heartbeat("push_nudges", "ok", f"fired {n}" if n else "")
        except Exception as exc:  # noqa: BLE001
            workers.heartbeat("push_nudges", "warn", str(exc)[:120])
            log.exception("nudge loop")
        await asyncio.sleep(60 - datetime.now().second)


# ─── routes ─────────────────────────────────────────────────────────

def _current_user():
    from .auth_sessions import current_user
    return current_user


class SubIn(BaseModel):
    subscription: Dict[str, Any]


class UnsubIn(BaseModel):
    endpoint: str


class NudgesIn(BaseModel):
    nudge_morning: Optional[str] = None
    nudge_evening: Optional[str] = None


def _valid_hhmm(v: Optional[str]) -> Optional[str]:
    if v in (None, ""):
        return None
    v = str(v).strip()
    try:
        h, m = v.split(":")
        assert 0 <= int(h) < 24 and 0 <= int(m) < 60
    except Exception:
        raise HTTPException(status_code=400, detail=f"time must be HH:MM, got {v!r}")
    return f"{int(h):02d}:{int(m):02d}"


@router.get("/vapid-public-key")
def vapid_public_key(user: Dict[str, Any] = Depends(_current_user())):
    return {"key": keys()["public"]}


@router.get("/status")
def status(user: Dict[str, Any] = Depends(_current_user())):
    with get_conn() as conn:
        row = conn.execute("SELECT nudge_morning, nudge_evening FROM user_profiles WHERE id = ?",
                           (user["id"],)).fetchone()
    subs = subscriptions_for(user["id"])
    return {
        "devices": [{"id": s["id"], "endpoint": s["endpoint"], "user_agent": s["user_agent"],
                     "created_at": s["created_at"], "last_ok_at": s["last_ok_at"]} for s in subs],
        "nudge_morning": row["nudge_morning"] if row else None,
        "nudge_evening": row["nudge_evening"] if row else None,
        "timezone": str(_tz()),
    }


@router.post("/subscribe")
def subscribe_route(body: SubIn, request: Request, user: Dict[str, Any] = Depends(_current_user())):
    if user.get("auth") == "api_token":
        raise HTTPException(status_code=403, detail="a browser session subscribes, not a token")
    try:
        sid = subscribe(user["id"], body.subscription, request.headers.get("user-agent", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": sid}


@router.post("/unsubscribe")
def unsubscribe_route(body: UnsubIn, user: Dict[str, Any] = Depends(_current_user())):
    return {"ok": unsubscribe(user["id"], body.endpoint)}


@router.post("/test")
def test_route(user: Dict[str, Any] = Depends(_current_user())):
    n = send(user["id"], title="Yorik", body="Push funktioniert." if (user.get("language") or "").startswith("de")
             else "Push works.", url="/r/home", tag="test")
    return {"ok": n > 0, "delivered": n}


@router.patch("/nudges")
def nudges_route(body: NudgesIn, user: Dict[str, Any] = Depends(_current_user())):
    if user.get("auth") == "api_token":
        raise HTTPException(status_code=403, detail="log in to change this")
    m = _valid_hhmm(body.nudge_morning)
    e = _valid_hhmm(body.nudge_evening)
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET nudge_morning = ?, nudge_evening = ? WHERE id = ?",
                     (m, e, user["id"]))
        conn.commit()
    return {"ok": True, "nudge_morning": m, "nudge_evening": e}
