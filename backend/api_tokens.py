"""Personal API tokens — how an external agent authenticates as a user.

A token is a random secret shown once at creation. Yorik stores only its
SHA-256 and the first eight characters (so the list in Settings can say
"yk_a1b2c3…"). A request carrying ``Authorization: Bearer yk_…`` resolves
to the owning user and from there on behaves exactly like that user's
browser session: same role, same workspace scoping, same confirm cards.

Tokens are meant for the MCP endpoint (``/mcp``) and for scripts that
talk to ``/api/*`` directly. They never elevate: a token cannot do
anything its owner cannot do in chat.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .database import get_conn

log = logging.getLogger("yorik.api_tokens")

TOKEN_PREFIX = "yk_"
_PREFIX_SHOWN = 8
_TOUCH_INTERVAL_S = 60.0

# token_hash -> monotonic time of the last last_used_at write. Keeps the
# hot path (every MCP request) from issuing an UPDATE each time.
_last_touch: dict[str, float] = {}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def create_token(user_id: str, name: str) -> tuple[str, dict[str, Any]]:
    """Mint a token for ``user_id``. Returns ``(plain_token, row)``; the
    plain token is not recoverable afterwards."""
    name = (name or "").strip() or "unnamed"
    plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO api_tokens (user_id, name, token_hash, token_prefix) "
            "VALUES (?, ?, ?, ?) RETURNING id, user_id, name, token_prefix, "
            "created_at, last_used_at, revoked_at",
            (user_id, name[:80], _hash(plain), plain[:_PREFIX_SHOWN]),
        )
        row = dict(cur.fetchone())
        conn.commit()
    log.info("api token created user_id=%s name=%s", user_id, name)
    return plain, _public(row)


def list_tokens(user_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, user_id, name, token_prefix, created_at, last_used_at, "
            "revoked_at FROM api_tokens WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    return [_public(dict(r)) for r in rows]


def revoke_token(token_id: int, *, user_id: str, is_admin: bool = False) -> bool:
    """Revoke one token. Owners can revoke their own; admins anyone's.
    Returns False when no matching live token exists."""
    with get_conn() as conn:
        if is_admin:
            cur = conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_now(), token_id),
            )
        else:
            cur = conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? "
                "AND revoked_at IS NULL",
                (_now(), token_id, user_id),
            )
        changed = cur.rowcount
        conn.commit()
    if changed:
        _last_touch.clear()
    return bool(changed)


def resolve_token(plain: str) -> Optional[dict[str, Any]]:
    """Map a bearer value to its user dict (same shape auth_sessions
    returns) or None. Revoked tokens and disabled users resolve to None."""
    if not plain or not plain.startswith(TOKEN_PREFIX):
        return None
    h = _hash(plain)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT t.token_hash, t.revoked_at, u.id, u.name, u.email, u.role, "
            "u.language, u.disabled "
            "FROM api_tokens t JOIN user_profiles u ON u.id = t.user_id "
            "WHERE t.token_hash = ?",
            (h,),
        ).fetchone()
    if not row:
        return None
    row = dict(row)
    if row.get("revoked_at") or row.get("disabled"):
        return None
    _touch(h)
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "language": row["language"],
        "disabled": row["disabled"],
        "auth": "api_token",
    }


def _touch(token_hash: str) -> None:
    now = time.monotonic()
    last = _last_touch.get(token_hash, 0.0)
    if now - last < _TOUCH_INTERVAL_S:
        return
    _last_touch[token_hash] = now
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ?",
                (_now(), token_hash),
            )
            conn.commit()
    except Exception:  # noqa: BLE001 — bookkeeping only
        log.debug("last_used_at update failed", exc_info=True)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "prefix": row["token_prefix"] + "…",
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
        "revoked": bool(row.get("revoked_at")),
    }


# ─── HTTP routes ────────────────────────────────────────────────────

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


class TokenCreate(BaseModel):
    name: str = Field(default="", max_length=80,
                      description="Label shown in Settings, e.g. 'Hermes on the workstation'")


def _current_user():
    from .auth_sessions import current_user
    return current_user


@router.get("")
def list_my_tokens(user: dict[str, Any] = Depends(_current_user())) -> list[dict[str, Any]]:
    return list_tokens(user["id"])


@router.post("", status_code=201)
def create_my_token(body: TokenCreate,
                    user: dict[str, Any] = Depends(_current_user())) -> dict[str, Any]:
    if user.get("auth") == "api_token":
        # A token must not mint further tokens; that is a browser-session action.
        raise HTTPException(status_code=403, detail="log in to create tokens")
    plain, row = create_token(user["id"], body.name)
    return {**row, "token": plain}


@router.delete("/{token_id}")
def revoke_my_token(token_id: int,
                    user: dict[str, Any] = Depends(_current_user())) -> dict[str, Any]:
    is_admin = (user.get("role") or "").lower() in ("admin", "platform_admin")
    if not revoke_token(token_id, user_id=user["id"], is_admin=is_admin):
        raise HTTPException(status_code=404, detail="token not found")
    return {"ok": True, "id": token_id}
