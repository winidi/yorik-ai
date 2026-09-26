"""Join by QR: invite a person into this household, and let their phone
open Yorik with a 4-digit PIN afterwards.

Admin side (signed in):
  POST   /api/invites            name, role → token, links, QR payload
  GET    /api/invites            open invites
  DELETE /api/invites/{id}       revoke one

Joining side (no session yet; /api/auth/* is outside the session gate,
and every route here also demands the home network or the tailnet):
  GET  /api/auth/invite/{token}          who is being invited (name, colour)
  POST /api/auth/invite/{token}/accept   name, colour, PIN → account,
                                          trusted device, signed in
  GET  /api/auth/device                  is this phone a known device?
  POST /api/auth/device-login            PIN on a known device → session

The token is 32 random bytes, stored only as its sha256, single use,
24 h. The device token is the same kind of secret in an httpOnly cookie
for 400 days. PIN attempts share the login throttle (per device and IP).
"""
from __future__ import annotations

import hashlib
import os
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import auth_sessions as _auth
from . import security_throttle as _throttle
from . import tailscale_local
from .auth_sessions import current_user, require_admin
from .database import get_conn
from .wlan_trust import is_trusted_lan_request

log = logging.getLogger("yorik.invites")

router = APIRouter(prefix="/api", tags=["invites"])

INVITE_TTL = timedelta(hours=24)
# The static public join page (served by Tailscale Funnel, not by Yorik).
from pathlib import Path as _Path
_JOIN_PAGE_DIR = _Path(__file__).resolve().parent.parent / "deploy" / "join-page"
DEVICE_COOKIE = "yorik_device"
DEVICE_TTL_DAYS = 400
_PIN_RE = re.compile(r"^\d{4}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_home(request: Request) -> None:
    if not is_trusted_lan_request(request):
        raise HTTPException(403, "Joining works only at home or over the family's Tailscale.")


# ── admin: create / list / revoke ─────────────────────────────────────

class InviteCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    role: str = Field(default="member")   # member | restricted
    color: Optional[str] = None


def _origin_fallback(request: Request) -> Optional[str]:
    """Without Tailscale, the address the admin is using right now works
    for phones on the same Wi-Fi (http://192.168.0.45:8000), unless it's
    this machine's own name for itself."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if not host or host in ("localhost", "127.0.0.1", "::1", "[::1]"):
        return None
    return str(request.base_url).rstrip("/")


def _links(token: str, name: str, ts_url: Optional[str], fallback: Optional[str] = None) -> dict[str, Any]:
    """Where the QR code points. With a public join page (Funnel) it goes
    there first and carries the rest in the #fragment, which browsers
    never send to a server; without one it points straight at Yorik."""
    base = tailscale_local.base_url() or fallback
    join_url = f"{base}/r/join?t={token}" if base else None
    page = tailscale_local.join_page_url()
    if page and join_url:
        frag = f"n={quote(name)}&u={quote(join_url, safe='')}"
        if ts_url:
            frag += f"&i={quote(ts_url, safe='')}"
        qr_url = f"{page}#{frag}"
    else:
        qr_url = join_url
    return {"join_url": join_url, "qr_url": qr_url, "join_page": bool(page and join_url),
            "tailscale_invite_url": ts_url}


@router.post("/invites", status_code=201, dependencies=[Depends(require_admin)])
def create_invite(body: InviteCreate, request: Request, user: dict = Depends(current_user)) -> dict[str, Any]:
    role = body.role if body.role in ("member", "restricted") else "member"
    color = body.color if body.color and _COLOR_RE.match(body.color) else None
    token = secrets.token_urlsafe(32)
    ts = tailscale_local.create_device_invite()
    ts_url = ts.get("url") if ts.get("ok") else None
    expires = _now() + INVITE_TTL
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO member_invites (token_hash, created_by, name, role, color, "
            "tailscale_invite_url, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (_hash(token), user["id"], body.name.strip(), role, color, ts_url, expires),
        ).fetchone()
        conn.commit()
    out = {"id": row["id"], "name": body.name.strip(), "role": role, "color": color,
           "expires_at": expires.isoformat(), "tailscale": ts.get("reason") or "ok"}
    out.update(_links(token, body.name.strip(), ts_url, _origin_fallback(request)))
    out["home_only"] = bool(out["join_url"]) and not tailscale_local.base_url()
    if not out["join_url"]:
        out["problem"] = ("Yorik doesn't know an address phones can reach. Open Yorik on this "
                          "computer by its network address (not localhost), or connect "
                          "Tailscale under Settings → System → Phones.")
    return out


@router.get("/invites", dependencies=[Depends(require_admin)])
def list_invites() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, role, color, created_at, expires_at, used_at FROM member_invites "
            "WHERE revoked_at IS NULL AND (used_at IS NOT NULL OR expires_at > now()) "
            "ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return [{"id": r["id"], "name": r["name"], "role": r["role"], "color": r["color"],
             "created_at": str(r["created_at"]), "expires_at": str(r["expires_at"]),
             "used": r["used_at"] is not None} for r in rows]


@router.delete("/invites/{invite_id}", dependencies=[Depends(require_admin)])
def revoke_invite(invite_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute("UPDATE member_invites SET revoked_at = now() WHERE id = ? AND used_at IS NULL",
                     (invite_id,))
        conn.commit()
    return {"ok": True}


# ── joining ───────────────────────────────────────────────────────────

def _open_invite(token: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, name, role, color, expires_at, used_at, revoked_at FROM member_invites "
            "WHERE token_hash = ?", (_hash(token),),
        ).fetchone()


def _invite_problem(row) -> Optional[str]:
    if not row:
        return "This invite doesn't exist. Ask for a new code."
    if row["revoked_at"] is not None:
        return "This invite was withdrawn. Ask for a new code."
    if row["used_at"] is not None:
        return "This invite was already used. If that was you, just open Yorik."
    exp = row["expires_at"]
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < _now():
        return "This invite has expired. Ask for a new code; it takes a few seconds."
    return None


@router.get("/auth/invite/{token}")
def invite_preview(token: str, request: Request) -> dict[str, Any]:
    _require_home(request)
    row = _open_invite(token)
    problem = _invite_problem(row)
    if problem:
        raise HTTPException(410, problem)
    from .people import PALETTE
    return {"name": row["name"], "color": row["color"], "role": row["role"], "palette": PALETTE}


class InviteAccept(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    color: Optional[str] = None
    pin: str
    email: Optional[str] = None


def _placeholder_email(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "member"
    # .invalid is reserved (RFC 2606): it can never receive mail.
    return f"{slug}-{secrets.token_hex(3)}@members.yorik.invalid"


def _new_device(user_id: str, label: str) -> str:
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute("INSERT INTO member_devices (user_id, token_hash, label, last_seen_at) "
                     "VALUES (?, ?, ?, now())", (user_id, _hash(token), label[:120]))
        conn.commit()
    return token


def _set_device_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(DEVICE_COOKIE, token, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https",
                        max_age=DEVICE_TTL_DAYS * 24 * 3600, path="/")


@router.post("/auth/invite/{token}/accept")
def accept_invite(token: str, body: InviteAccept, request: Request, response: Response) -> dict[str, Any]:
    _require_home(request)
    if not _PIN_RE.match(body.pin or ""):
        raise HTTPException(400, "The PIN is 4 digits.")
    email = (body.email or "").strip().lower()
    if email and ("@" not in email or len(email) > 200):
        raise HTTPException(400, "That email address doesn't look right.")

    # Claim the invite first, atomically: two phones scanning the same
    # code can't both become members.
    with get_conn() as conn:
        claimed = conn.execute(
            "UPDATE member_invites SET used_at = now() WHERE token_hash = ? AND used_at IS NULL "
            "AND revoked_at IS NULL AND expires_at > now() RETURNING id, role, color",
            (_hash(token),),
        ).fetchone()
        conn.commit()
    if not claimed:
        raise HTTPException(410, _invite_problem(_open_invite(token)) or "This invite can't be used.")

    from . import users as _users
    from .auth_sessions import get_user_by_email
    if email and get_user_by_email(email):
        with get_conn() as conn:  # give the invite back
            conn.execute("UPDATE member_invites SET used_at = NULL WHERE id = ?", (claimed["id"],))
            conn.commit()
        raise HTTPException(409, "Someone in this household already uses that email. Leave it empty or pick another.")

    # A random password nobody types: this person opens Yorik with the
    # PIN on their device. The same password seeds their photo and
    # document archive accounts; it's kept encrypted so the Photos
    # setup can sign them in there later.
    password = secrets.token_urlsafe(18)
    created = _users.create_user(_users.UserCreate(
        email=email or _placeholder_email(body.name), name=body.name.strip(),
        role=claimed["role"], password=password,
    ))
    uid = created["id"]
    from . import credential_store
    credential_store.put(f"member_pw:{uid}", {"password": password})
    _auth.set_pin(uid, body.pin)
    color = body.color if body.color and _COLOR_RE.match(body.color) else claimed["color"]
    if color:
        from .people import set_color
        set_color(uid, color)
    with get_conn() as conn:
        conn.execute("UPDATE member_invites SET used_by = ? WHERE id = ?", (uid, claimed["id"]))
        # They chose name and colour just now; the rest of the first-run
        # wizard (address, business) isn't theirs to answer. The Home
        # checklist takes it from here.
        conn.execute("UPDATE user_profiles SET onboarded_at = current_timestamp WHERE id = ?", (uid,))
        conn.commit()

    device_token = _new_device(uid, (request.headers.get("user-agent") or "phone"))
    _set_device_cookie(response, request, device_token)
    sid = _auth.create_session(uid, user_agent=request.headers.get("user-agent", ""),
                               ip=request.client.host if request.client else None)
    _auth._set_cookie(response, sid, request)
    log.info("invite accepted invite_id=%s user_id=%s", claimed["id"], uid)
    return {"ok": True, "user": {"id": uid, "name": body.name.strip()}}


def _device_user(request: Request):
    token = request.cookies.get(DEVICE_COOKIE)
    if not token:
        return None, None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT d.id AS device_id, u.id, u.name, u.first_name, u.disabled, u.color "
            "FROM member_devices d JOIN user_profiles u ON u.id = d.user_id "
            "WHERE d.token_hash = ? AND d.revoked_at IS NULL", (_hash(token),),
        ).fetchone()
    return row, token


@router.get("/auth/device")
def device_status(request: Request) -> dict[str, Any]:
    row, _ = _device_user(request)
    if not row or row["disabled"]:
        return {"known": False}
    from .people import avatar_url, color_for
    first = row["first_name"] or (row["name"] or "").split(" ")[0]
    return {"known": True, "first_name": first, "color": color_for(str(row["id"]), row["color"]),
            "avatar_url": avatar_url(str(row["id"]))}


class DeviceLogin(BaseModel):
    pin: str


@router.post("/auth/device-login")
def device_login(body: DeviceLogin, request: Request, response: Response) -> dict[str, Any]:
    _require_home(request)
    row, token = _device_user(request)
    if not row or row["disabled"]:
        raise HTTPException(401, "This phone isn't set up for Yorik yet.")
    ip = request.client.host if request.client else "unknown"
    key = f"device:{row['device_id']}"
    allowed, retry, _reason = _throttle.check_login_allowed(key, ip)
    if not allowed:
        raise HTTPException(429, "Too many wrong PINs. Wait a moment and try again.",
                            headers={"Retry-After": str(retry or 60)})
    if not _auth.verify_pin(str(row["id"]), body.pin or ""):
        _throttle.record_login_failure(key, ip)
        raise HTTPException(401, "That PIN isn't right.")
    _throttle.clear_login_failures(key)
    with get_conn() as conn:
        conn.execute("UPDATE member_devices SET last_seen_at = now() WHERE id = ?", (row["device_id"],))
        conn.commit()
    sid = _auth.create_session(str(row["id"]), user_agent=request.headers.get("user-agent", ""), ip=ip)
    _auth._set_cookie(response, sid, request)
    _auth.touch_login(str(row["id"]))
    return {"ok": True}


@router.post("/auth/device-forget")
def device_forget(request: Request, response: Response) -> dict[str, Any]:
    """'Not you?' on the PIN screen: this browser forgets the device."""
    row, _ = _device_user(request)
    if row:
        with get_conn() as conn:
            conn.execute("UPDATE member_devices SET revoked_at = now() WHERE id = ?", (row["device_id"],))
            conn.commit()
    response.delete_cookie(DEVICE_COOKIE, path="/")
    return {"ok": True}


# ── settings card: phones and Tailscale ───────────────────────────────

@router.get("/invites/setup", dependencies=[Depends(require_admin)])
def invite_setup() -> dict[str, Any]:
    me = tailscale_local.self_info()
    return {
        "tailscale_running": bool(me),
        "dns_name": (me or {}).get("dns_name"),
        "base_url": tailscale_local.base_url(),
        "join_page_url": tailscale_local.join_page_url(),
        "join_page_port": tailscale_local.JOIN_PAGE_PORT,
        "api_configured": tailscale_local.api_configured(),
        "join_page_dir": str(_JOIN_PAGE_DIR),
        "runtime": os.getenv("YORIK_RUNTIME") or "classic",
        "login": tailscale_local.login_state(),
    }


class TailscaleAccess(BaseModel):
    api_key: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


@router.put("/invites/tailscale-access", dependencies=[Depends(require_admin)])
def save_tailscale_access(body: TailscaleAccess) -> dict[str, Any]:
    from . import credential_store
    data = {k: v.strip() for k, v in body.model_dump().items() if v and v.strip()}
    if not data:
        credential_store.delete("tailscale_api")
        return {"ok": True, "message": "Removed."}
    credential_store.put("tailscale_api", data)
    tailscale_local.forget_cache()
    return tailscale_local.check_api()


@router.post("/invites/setup/refresh", dependencies=[Depends(require_admin)])
def refresh_setup() -> dict[str, Any]:
    tailscale_local.forget_cache()
    return invite_setup()
