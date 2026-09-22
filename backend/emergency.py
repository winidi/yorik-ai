"""Emergency access — "Zugriff auf alles".

Yorik has no admin exception for seeing: personal data is visible to
its owner and to whoever it was shared with. There is one situation
where the household needs a way past that: an adult is in hospital,
missing, or dead, and the other adult must reach the contracts, the
calendar, the documents. Decided with Dirk on 2026-09-22: the adults
can switch that on, but not casually, and the others learn of it.

What it takes: an adult account (any role but `restricted`), a reason
in words, the person's own password again, and a duration of at most
24 hours. What happens: a row in `emergency_access` (the log every
adult can read in Settings), a notification to every other adult the
moment it starts and the moment it ends, and — while it runs — the
spaces model shows that person every space in the house and the
Paperless reads run with the admin token.

What it does NOT open: another person's mail, WhatsApp and chats with
Yorik (those filters are per row and stay), and other people's Immich
libraries. That is a known limit, written down in the audit.

`active()` is on the hot path of every list filter, so it caches for
a few seconds.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .database import get_conn

log = logging.getLogger("yorik.emergency")

MAX_HOURS = 24
MIN_REASON_CHARS = 10
_CACHE_TTL_S = 5.0
_cache: Dict[str, tuple[float, Optional[Dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _forget(user_id: Any) -> None:
    with _cache_lock:
        _cache.pop(str(user_id), None)


def active(user_id: Any) -> Optional[Dict[str, Any]]:
    """The person's running emergency access, or None. Cached briefly."""
    if user_id is None:
        return None
    key = str(user_id)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id, user_id, reason, started_at, expires_at FROM emergency_access "
                "WHERE user_id = ? AND ended_at IS NULL AND expires_at > ? "
                "ORDER BY started_at DESC LIMIT 1",
                (key, _now()),
            ).fetchone()
        out = dict(row) if row else None
    except Exception as exc:  # noqa: BLE001 — table missing on an old install: no emergency
        log.debug("emergency.active(%s): %s", user_id, exc)
        out = None
    with _cache_lock:
        _cache[key] = (now, out)
    return out


def _adults(exclude: Any = None) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, role FROM user_profiles "
            "WHERE (disabled = 0 OR disabled IS NULL) AND lower(role) <> 'restricted' ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows if str(r["id"]) != str(exclude)]


def _notify_adults(actor: Dict[str, Any], title: str, body: str) -> None:
    from . import notifications
    for adult in _adults(exclude=actor["id"]):
        try:
            notifications.create(user_id=str(adult["id"]), kind="emergency_access", title=title, body=body,
                                 navigate_to="/r/settings")
        except Exception as exc:  # noqa: BLE001 — the access still counts, the log has it
            log.warning("emergency: could not notify %s: %s", adult.get("name"), exc)


def start(user: Dict[str, Any], *, reason: str, hours: int, password: str) -> Dict[str, Any]:
    """Switch the emergency access on for `user`. Raises ValueError with
    a message for the person when a condition is not met."""
    from . import auth_sessions as _auth
    role = (user.get("role") or "").lower()
    if role == "restricted":
        raise ValueError("emergency access is for the adults of the household")
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_CHARS:
        raise ValueError(f"say why, in at least {MIN_REASON_CHARS} characters — the others will read it")
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        raise ValueError("hours must be a number")
    if not 1 <= hours <= MAX_HOURS:
        raise ValueError(f"between 1 and {MAX_HOURS} hours")
    with get_conn() as conn:
        row = conn.execute("SELECT password_hash FROM user_profiles WHERE id = ?", (user["id"],)).fetchone()
    if not row or not _auth.verify_password(password or "", row["password_hash"]):
        raise ValueError("wrong password")

    started = datetime.now().replace(microsecond=0)
    expires = started + timedelta(hours=hours)
    with get_conn() as conn:
        conn.execute("UPDATE emergency_access SET ended_at = ? WHERE user_id = ? AND ended_at IS NULL",
                     (started.isoformat(), user["id"]))
        rid = conn.execute(
            "INSERT INTO emergency_access (user_id, reason, started_at, expires_at) VALUES (?, ?, ?, ?) RETURNING id",
            (user["id"], reason, started.isoformat(), expires.isoformat()),
        ).fetchone()["id"]
        conn.commit()
    _forget(user["id"])
    name = user.get("name") or "Someone"
    log.warning("EMERGENCY ACCESS ON: %s (%s) until %s — %s", name, user["id"], expires.isoformat(), reason)
    _notify_adults(user, f"{name} hat den Notfall-Zugriff eingeschaltet",
                   f"Bis {expires.strftime('%d.%m. %H:%M')} sieht {name} alles im Haushalt. Grund: {reason}")
    return {"id": int(rid), "user_id": str(user["id"]), "reason": reason,
            "started_at": started.isoformat(), "expires_at": expires.isoformat(), "ended_at": None}


def end(user: Dict[str, Any], access_id: int) -> bool:
    """End one's own emergency access early. False when it is not the
    person's own running access."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM emergency_access WHERE id = ? AND user_id = ? AND ended_at IS NULL",
                           (int(access_id), user["id"])).fetchone()
        if not row:
            return False
        conn.execute("UPDATE emergency_access SET ended_at = ? WHERE id = ?", (_now(), int(access_id)))
        conn.commit()
    _forget(user["id"])
    name = user.get("name") or "Someone"
    log.warning("EMERGENCY ACCESS OFF: %s (%s)", name, user["id"])
    _notify_adults(user, f"{name} hat den Notfall-Zugriff beendet", "Der Zugriff auf alles ist wieder aus.")
    return True


def history(limit: int = 50) -> List[Dict[str, Any]]:
    """Every emergency access ever, newest first — the log the adults
    can read. Whoever switched it on is named."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT e.id, e.user_id, u.name AS user_name, e.reason, e.started_at, e.expires_at, e.ended_at "
            "FROM emergency_access e LEFT JOIN user_profiles u ON u.id = e.user_id "
            "ORDER BY e.started_at DESC LIMIT ?", (int(limit),),
        ).fetchall()
    now = _now()
    out = []
    for r in rows:
        d = dict(r)
        d["user_id"] = str(d["user_id"])
        d["active"] = d["ended_at"] is None and d["expires_at"] > now
        out.append(d)
    return out
