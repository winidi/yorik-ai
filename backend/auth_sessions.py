"""Authentication + session management for Yorik (multi-user wave).

Lives alongside the existing role-based auth.py (which stays as the
ACL primitive). This module handles:

  - Password hashing (bcrypt)
  - Session creation / lookup / revocation (server-side, opaque token)
  - Cookie helpers (HttpOnly + SameSite=Lax)
  - FastAPI dependency `current_user` that any endpoint can require

Cookie strategy:
  - Name: yorik_session
  - Value: 32-byte URL-safe random (~43 chars), opaque to the client
  - HttpOnly + SameSite=Lax + Path=/ — no JS access, sent on same-site
    requests including from iframes Yorik mounts
  - Secure flag set automatically when request URL is HTTPS

Why server-side sessions vs JWT: revocation is a 1-row DELETE,
device-listing is a SELECT, and we never have to worry about
re-signing or key rotation. JWTs are nice when crossing service
boundaries; Yorik is one server, so sessions are simpler.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import os as _os
import bcrypt
import jwt as _pyjwt
from fastapi import Cookie, Depends, HTTPException, Request, Response

from .database import get_conn

# Phase E §1: JWT path alongside cookies. Strategy B from the
# masterplan — Yorik continues issuing yorik_session cookies AND
# accepts Supabase Auth JWTs in the Authorization header. Both
# resolve to the same user via user_profiles.id = auth.users.id
# (UUID-matched in the Phase E bootstrap migration). Once the
# frontend has fully moved to Supabase Auth, the cookie path here
# becomes dead code and gets removed (Strategy C).
_SUPABASE_JWT_SECRET = _os.getenv("YORIK_SUPABASE_JWT_SECRET")
if not _SUPABASE_JWT_SECRET:
    # Fallback: read straight from infra/supabase/docker/.env so the
    # workstation install doesn't need a duplicated env var. Production
    # cloud installs should set YORIK_SUPABASE_JWT_SECRET explicitly.
    from pathlib import Path as _Path
    _env_file = _Path(__file__).resolve().parent.parent / "infra/supabase/docker/.env"
    if _env_file.exists():
        for _line in _env_file.read_text().splitlines():
            if _line.startswith("JWT_SECRET="):
                _SUPABASE_JWT_SECRET = _line.split("=", 1)[1].strip()
                break

_auth_log = logging.getLogger("yorik.auth")

COOKIE_NAME = "yorik_session"
SESSION_TTL_DAYS = 30
# Idle-timeout: how long a session can sit unused before we kick it
# out even though its absolute expiry hasn't fired yet. Defense
# against laptop-left-open + tab-left-open-for-months scenarios.
# Override via env so a security-sensitive box can tighten to e.g.
# 1 day; a kiosk where convenience matters can keep the default.
SESSION_IDLE_TIMEOUT_HOURS = int(_os.getenv("YORIK_SESSION_IDLE_HOURS", str(24 * 7)))
# When a session is within this window of expiry, auto-extend on use.
SESSION_REFRESH_DAYS = 7


# ───────────────────────── password hashing ────────────────────────────

def hash_password(password: str) -> str:
    """Bcrypt hash. Cost factor 12 = ~250ms on a modern laptop, which
    is the right tradeoff for an interactive login (slow enough to
    deter brute force, fast enough to not feel laggy)."""
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, hashed: Optional[str]) -> bool:
    if not hashed or not password:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ───────────────────────── session lifecycle ───────────────────────────

def create_session(user_id: str, user_agent: Optional[str] = None,
                   ip: Optional[str] = None, ttl_days: int = SESSION_TTL_DAYS,
                   wall_device_id: Optional[str] = None) -> str:
    """Insert a new session and return its opaque id (to be set as cookie).

    If wall_device_id is provided AND that UUID is in the
    trusted_kiosk_devices table, the session is auto-flagged
    is_kiosk with the saved kiosk config (album, etc.) — that's how
    the YorikWall Android wrapper avoids the per-login "go to
    Settings → Devices → tick kiosk" dance.
    """
    sid = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=ttl_days)).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, expires_at, user_agent, ip_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, user_id, expires, (user_agent or "")[:200], ip),
        )
        # Apply trusted-kiosk policy if this device has been marked
        # trusted by an admin. The policy lives in trusted_kiosk_devices
        # keyed by the wrapper-generated UUID; we copy the relevant
        # fields onto the session row and bump trusted_until so the
        # PIN-switch flow keeps working on this session.
        if wall_device_id:
            row = conn.execute(
                "SELECT device_label, kiosk_album_id, kiosk_show_today, "
                "       kiosk_block_phrases "
                "FROM trusted_kiosk_devices WHERE device_id = ?",
                (wall_device_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE sessions SET "
                    "  is_kiosk = 1, "
                    "  device_label = COALESCE(?, device_label), "
                    "  kiosk_album_id = ?, "
                    "  kiosk_show_today_photos = ?, "
                    "  kiosk_block_phrases = ?, "
                    # db_shim translates the bare `datetime('now')` form
                    # but not the two-arg variant. Use to_char + interval
                    # directly so this works on Postgres without a shim
                    # round-trip. Same shape as the rest of the code's
                    # 'YYYY-MM-DD HH:MM:SS' text-column convention.
                    "  trusted_until = to_char(now() + interval '365 days', 'YYYY-MM-DD HH24:MI:SS'), "
                    "  expires_at = to_char(now() + interval '365 days', 'YYYY-MM-DD HH24:MI:SS') "
                    "WHERE id = ?",
                    (row["device_label"], row["kiosk_album_id"],
                     row["kiosk_show_today"],
                     row["kiosk_block_phrases"], sid),
                )
                conn.execute(
                    "UPDATE trusted_kiosk_devices SET last_seen_at = CURRENT_TIMESTAMP "
                    "WHERE device_id = ?",
                    (wall_device_id,),
                )
        conn.commit()
    return sid


def create_ephemeral_session(user_id: str, *, ttl_seconds: int,
                              user_agent: Optional[str] = None,
                              ip: Optional[str] = None) -> str:
    """Same shape as create_session but with a SECOND-grain TTL.
    Used for PIN-switch on kiosk: a 5-minute window where API calls
    run as the picked user, after which the cookie expires and the
    tablet falls back to the avatar+PIN picker."""
    sid = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(seconds=int(ttl_seconds))).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, expires_at, user_agent, ip_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, user_id, expires, (user_agent or "")[:200], ip),
        )
        conn.commit()
    return sid


def delete_session(sid: str) -> None:
    if not sid:
        return
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        conn.commit()


def revoke_all_sessions(user_id: str) -> int:
    """Used when password is reset or admin disables a user."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()
        return cur.rowcount or 0


def get_user_for_session(sid: Optional[str], ip: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Look up the user behind a session cookie. Returns None if the
    session is missing, expired, or the user is disabled. Touches
    last_seen_at + sliding-window-extends expiry if within the refresh
    threshold (so an active user never gets logged out mid-session)."""
    if not sid:
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT s.id, s.user_id, s.expires_at, s.last_seen_at, "
            "       u.name, u.email, u.role, u.disabled, u.language, "
            "       u.country, u.address_street, u.address_postcode, u.address_city, "
            "       u.phone, u.business_name, u.tax_id, u.iban, u.onboarded_at, "
            "       u.confirm_mutations, u.voice_ack_enabled, u.dev_mode, "
            "       u.default_doc_visibility, u.first_name, u.last_name, "
            "       u.signature_data_url, u.pin_set_at "
            "FROM sessions s JOIN user_profiles u ON u.id = s.user_id "
            "WHERE s.id = ?",
            (sid,),
        ).fetchone()
        if not row:
            return None
        if row["disabled"]:
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return None
        if expires < now:
            conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
            conn.commit()
            return None
        # Idle-timeout: if last_seen_at is older than the idle threshold,
        # treat as expired even though the absolute expiry hasn't fired.
        # Closes the "laptop open on coffee shop table for 3 weeks"
        # gap that absolute-only expiry leaves wide open.
        try:
            last_seen = datetime.fromisoformat(row["last_seen_at"])
            idle = now - last_seen
            if idle > timedelta(hours=SESSION_IDLE_TIMEOUT_HOURS):
                conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
                conn.commit()
                return None
        except (TypeError, ValueError):
            pass  # malformed last_seen_at — treat as fresh, will be re-stamped below
        # Touch + maybe extend.
        new_expiry: Optional[str] = None
        if (expires - now) < timedelta(days=SESSION_REFRESH_DAYS):
            new_expiry = (now + timedelta(days=SESSION_TTL_DAYS)).isoformat(timespec="seconds")
        params = [now.isoformat(timespec="seconds"), ip or "", sid]
        sql = "UPDATE sessions SET last_seen_at=?, ip_seen=?"
        if new_expiry:
            sql += ", expires_at=?"
            params.insert(2, new_expiry)
        sql += " WHERE id=?"
        conn.execute(sql, params)
        conn.commit()
    return {
        "id":               row["user_id"],
        "name":             row["name"],
        "email":            row["email"],
        "role":             row["role"],
        "language":         row["language"],
        "country":          row["country"],
        "address_street":   row["address_street"],
        "address_postcode": row["address_postcode"],
        "address_city":     row["address_city"],
        "phone":            row["phone"],
        "business_name":    row["business_name"],
        "tax_id":           row["tax_id"],
        "iban":             row["iban"],
        "onboarded_at":     row["onboarded_at"],
        "confirm_mutations": bool(row["confirm_mutations"]),
        "voice_ack_enabled": bool(row["voice_ack_enabled"]),
        "dev_mode":          bool(row["dev_mode"]),
        "default_doc_visibility": row["default_doc_visibility"] or "private",
        "first_name":         row["first_name"] or "",
        "last_name":          row["last_name"] or "",
        "signature_data_url": row["signature_data_url"] or "",
        # Surfaces "PIN set on …" in Settings → Profile → Kiosk PIN.
        # The hash itself NEVER leaves the server — clients only see
        # whether a PIN exists (truthiness of pin_set_at).
        "pin_set_at":         row["pin_set_at"],
    }


# ───────────────────────── user lookups ────────────────────────────────

def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    if not email:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, email, role, password_hash, disabled, language "
            "FROM user_profiles WHERE LOWER(email) = LOWER(?)",
            (email.strip(),),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, email, role, disabled, language FROM user_profiles WHERE id=?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def set_password(user_id: str, new_password: str) -> None:
    """Set or reset a user's password. Also kicks every existing session
    for this user (forces re-login everywhere) — standard security
    hygiene on password change."""
    h = hash_password(new_password)
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_profiles SET password_hash=?, password_set_at=? WHERE id=?",
            (h, now, user_id),
        )
        conn.commit()
    revoke_all_sessions(user_id)


def has_any_password() -> bool:
    """True if at least one user has a password set. Drives the
    first-run-setup decision: if False, /api/auth/setup is allowed
    to create the initial credentials; once True, that endpoint
    requires an authenticated admin."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM user_profiles WHERE password_hash IS NOT NULL"
        ).fetchone()
    return (row["n"] or 0) > 0


def touch_login(user_id: str) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET last_login_at=? WHERE id=?", (now, user_id))
        conn.commit()


# ───────────────────────── PIN (kiosk fallback) ────────────────────────
# A per-user 4-digit PIN used ONLY on trusted-device sessions (kiosks)
# to swap the active user without typing the full password. NEVER
# accepted on a fresh / untrusted browser session — full password is
# still required there. See trusted_until on the sessions row +
# /api/auth/pin-switch in main.py for the enforcement seam.

_PIN_LEN = 4  # exact — UI enforces. Server double-checks below.


def set_pin(user_id: str, pin: str) -> None:
    """Set or replace a user's 4-digit PIN. bcrypt-hashed at rest with
    the same cost factor as passwords. Existing sessions stay valid —
    PIN change doesn't kick anyone; the PIN is a CONVENIENCE on
    trusted devices, not a credential strong enough to gate session
    issue. Raises ValueError on malformed input so the route handler
    can return 422.

    Direct bcrypt (not hash_password) because hash_password enforces
    a minimum-8-char password policy that doesn't apply to PINs —
    PINs deliberately trade strength for typing speed on a touch
    keypad and are gated behind a trusted-device session anyway.
    """
    if not isinstance(pin, str) or len(pin) != _PIN_LEN or not pin.isdigit():
        raise ValueError(f"PIN must be exactly {_PIN_LEN} digits")
    h = bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_profiles SET pin_hash=?, pin_set_at=? WHERE id=?",
            (h, now, user_id),
        )
        conn.commit()


def clear_pin(user_id: str) -> None:
    """Remove a user's PIN. Future kiosk fallbacks for this user will
    skip the PIN step (and silently sign them in, on a single-family
    kiosk — admins can still require PIN-per-user via Settings)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE user_profiles SET pin_hash=NULL, pin_set_at=NULL WHERE id=?",
            (user_id,),
        )
        conn.commit()


def verify_pin(user_id: str, pin: str) -> bool:
    """Check a PIN against the stored hash. Returns False for users
    who haven't set one — the caller should treat that as "PIN check
    failed" and surface "set a PIN in Settings" rather than letting
    PIN-less users authenticate by default. Constant-time on bcrypt
    failure path; same shape as verify_password()."""
    if not isinstance(pin, str) or len(pin) != _PIN_LEN or not pin.isdigit():
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pin_hash FROM user_profiles WHERE id=?", (user_id,),
        ).fetchone()
    if not row or not row["pin_hash"]:
        # No PIN set — keep timing similar to a real verify by running
        # bcrypt against a dummy hash. Otherwise an attacker can probe
        # "does this user have a PIN" via timing.
        verify_password(pin, "$2b$12$0000000000000000000000.dummyhashformusttakeequaltime")
        return False
    return verify_password(pin, row["pin_hash"])


# ───────────────────────── trusted-device sessions ─────────────────────
# Distinct from regular 30-day sessions. A trusted session has
# trusted_until set (now+365d) and accepts PIN-switching via
# /api/auth/pin-switch. Kiosk sessions are ALWAYS trusted; the
# inverse isn't true (a trusted desktop session is also possible for
# households who want PIN-switching without a tablet).


def session_is_trusted(sid: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT trusted_until FROM sessions WHERE id=?", (sid,),
        ).fetchone()
    if not row or not row["trusted_until"]:
        return False
    try:
        until = datetime.fromisoformat(row["trusted_until"])
    except ValueError:
        return False
    return until > datetime.now(timezone.utc).replace(tzinfo=None)


def session_is_kiosk(sid: str) -> bool:
    """Quick check for the /api/ambient/* route guards. Reads only the
    is_kiosk column, doesn't touch user data, doesn't bump last_seen."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_kiosk FROM sessions WHERE id=?", (sid,),
        ).fetchone()
    return bool(row and row["is_kiosk"])


def kiosk_session_meta(sid: str) -> Optional[dict[str, Any]]:
    """Read kiosk-relevant fields for a session in one query. Used by
    /api/ambient/slideshow + /api/ambient/idle. Returns None when the
    session isn't a kiosk."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, kiosk_album_id, device_label, "
            "       kiosk_show_today_photos, kiosk_block_phrases "
            "FROM sessions WHERE id=? AND is_kiosk=1",
            (sid,),
        ).fetchone()
    return dict(row) if row else None


def is_trusted_kiosk_device(device_id: str) -> bool:
    """True when the device UUID is in trusted_kiosk_devices.
    Used by pin-switch and the kiosk read-path fallback to recognize
    a wall whose cookie session isn't is_kiosk=1 (typical after a
    PIN-switch — the ephemeral session replaces the kiosk cookie but
    the wall itself is still trusted)."""
    if not device_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM trusted_kiosk_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
    return bool(row)


def trusted_kiosk_device_meta(device_id: str) -> Optional[dict[str, Any]]:
    """Read the persisted kiosk policy for a wall whose UUID is in
    trusted_kiosk_devices. Returns a dict shaped IDENTICALLY to
    kiosk_session_meta — same keys — so the slideshow / idle endpoints
    can use it interchangeably. Column name `kiosk_show_today` (on the
    devices table) is normalized to `kiosk_show_today_photos` (on the
    sessions table) here so callers don't branch."""
    if not device_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, device_label, kiosk_album_id, "
            "       kiosk_show_today, "
            "       kiosk_block_phrases "
            "FROM trusted_kiosk_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "user_id":                  row["user_id"],
        "device_label":             row["device_label"],
        "kiosk_album_id":           row["kiosk_album_id"],
        "kiosk_show_today_photos":  row["kiosk_show_today"],
        "kiosk_block_phrases":      row["kiosk_block_phrases"],
    }


# ───────────────────────── FastAPI dependencies ────────────────────────

def _set_cookie(response: Response, sid: str, request: Request) -> None:
    secure = request.url.scheme == "https"
    response.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        path="/",
    )


def _clear_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _get_user_from_jwt(token: str) -> Optional[dict[str, Any]]:
    """Phase E §1: validate a Supabase Auth JWT and return the matching
    Yorik user dict. The JWT's `sub` claim is the auth.users.id UUID,
    which equals user_profiles.id (Phase E bootstrap pinned them together
    via the FK).

    Returns None for any validation failure — caller should fall back
    to the cookie path before deciding the request is unauthenticated.
    """
    if not _SUPABASE_JWT_SECRET or not token:
        return None
    try:
        claims = _pyjwt.decode(
            token,
            _SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",   # GoTrue's default audience
        )
    except _pyjwt.PyJWTError:
        return None
    user_id = claims.get("sub")
    if not user_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, email, role, language, disabled "
            "FROM user_profiles WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    user = dict(row)
    if user.get("disabled"):
        return None
    return user


def current_user_optional(
    request: Request,
    yorik_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> Optional[dict[str, Any]]:
    """Returns the user dict or None. Endpoints that can be called
    anonymously (e.g. /api/auth/login itself) use this.

    Phase E §1: try the Authorization Bearer header first (Supabase
    JWT — the future path), fall back to the yorik_session cookie
    (legacy — still works during the B→C transition)."""
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        user = _get_user_from_jwt(token)
        if user:
            return user
    if not yorik_session:
        return None
    return get_user_for_session(yorik_session, ip=request.client.host if request.client else None)


def current_user(
    request: Request,
    user: Optional[dict[str, Any]] = Depends(current_user_optional),
) -> dict[str, Any]:
    """Required-auth dependency — raises 401 if not logged in. Most
    application endpoints depend on this."""
    if not user:
        _auth_log.warning("auth.deny no_session path=%s",
                          request.url.path,
                          extra={"event": "deny_no_session", "path": request.url.path})
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def require_admin(request: Request, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    # Phase C T10: `platform_admin` is the new global admin role (Dirk's
    # role on the founder install); `admin` is the per-workspace admin.
    # Both pass admin-gated endpoints — per-workspace scoping happens at
    # the row layer (spaces.user_visible_space_ids, etc.). Without
    # accepting platform_admin here, Dirk loses access to Settings →
    # Users right after the role migration.
    role = (user.get("role") or "").lower()
    if role not in ("platform_admin", "admin"):
        _auth_log.warning("auth.deny not_admin user_id=%s role=%s path=%s",
                          user.get("id"), user.get("role"), request.url.path,
                          extra={"event": "deny_not_admin", "user_id": user.get("id"),
                                 "role": user.get("role"), "path": request.url.path})
        raise HTTPException(status_code=403, detail="admin only")
    return user


def current_role(user: dict[str, Any] = Depends(current_user)) -> str:
    """The calling user's role, derived from their session cookie. Use
    this as a drop-in replacement for `role: str = Query("admin")` on
    endpoints that still pass a role string through to ROW-level helpers
    like `normalize_role`, `require_role`, `apply_filter`. The role can
    NOT be overridden by the client — what the cookie says, goes."""
    return str(user.get("role") or "")
