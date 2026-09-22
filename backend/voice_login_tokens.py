"""Single-use voice-login tokens.

The /api/ask-voice/stream endpoint mints one of these when ECAPA
identifies the speaker against an enrolled profile; the frontend
redeems it at /api/auth/voice-login to swap the kiosk session over
to the identified user. Same end state as a PIN-switch, but without
the user typing.

Why a separate redemption endpoint instead of swapping the session
in-line on the streaming endpoint: the auto-heal trust cascade we
ripped out 2026-06-07 burned us by letting one endpoint mint sessions
on behalf of another. Keeping redemption in its own LAN-gated route
means voice-ID and session swap have separate audit logs, separate
rate limits, and separate threat models.

Token shape: HMAC-bound to (profile_id, device_uuid, source_sid, nonce,
issued_at). A captured token can't be replayed from a different device,
from a different browser session, or against a different profile, and a
single nonce can only redeem once before the in-memory consumed set
rejects it.

The HMAC secret is per install: generated once and kept in the
credential store (or set with HOMEOS_VOICE_LOGIN_SECRET). Until
2026-09-22 it defaulted to a constant known from the source, and
verify() never checked the source session, so anyone on the LAN who
knew a wall device's id could mint a token for any profile and get a
full session as that person (audit 4.1).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import OrderedDict
from typing import Optional

# Token lives long enough for the round-trip from the streaming
# response to the frontend's redemption POST. 60s is generous —
# tight enough that a leaked token in a network capture expires
# faster than someone can notice + replay it.
TOKEN_TTL_SECONDS = 60

# In-memory single-use ledger. Survives only as long as the worker
# process — after a restart, the worst case is one replay per boot,
# which is acceptable because the session created by the original
# redemption is still the active one.
_consumed: "OrderedDict[str, float]" = OrderedDict()
_CONSUMED_MAX = 1024


_STORE_KEY = "voice_login"
_cached_secret: Optional[bytes] = None


def _secret() -> bytes:
    """Per-install HMAC secret: HOMEOS_VOICE_LOGIN_SECRET from config.env
    if set, else a random one generated on first use and kept in the
    credential store. Without a usable store (no master key yet) the
    secret lives for this process only — a token minted before a
    restart is then rejected, which is the safe direction."""
    global _cached_secret
    override = os.getenv("HOMEOS_VOICE_LOGIN_SECRET", "").strip()
    if override:
        return hashlib.sha256(override.encode("utf-8")).digest()
    if _cached_secret:
        return _cached_secret
    fresh = secrets.token_bytes(32)
    try:
        from . import credential_store
        stored = (credential_store.get(_STORE_KEY) or {}).get("secret")
        if stored:
            _cached_secret = base64.urlsafe_b64decode(stored)
            return _cached_secret
        credential_store.put(_STORE_KEY, {"secret": base64.urlsafe_b64encode(fresh).decode("ascii")})
    except Exception:  # noqa: BLE001 — store unavailable: per-process secret
        pass
    _cached_secret = fresh
    return _cached_secret


def _b64encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def mint(*, profile_id: int, device_uuid: str, source_sid: str) -> str:
    """Issue a single-use token good for ~60 seconds.

    Bound to the device UUID so the same token can't redeem from
    another tablet, and to the source session id so it can't outlive
    its origin session.
    """
    payload = {
        "p": int(profile_id),
        "d": device_uuid,
        "s": source_sid or "",
        "t": int(time.time()),
        "n": secrets.token_urlsafe(12),
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_secret(), body, hashlib.sha256).digest()
    return f"{_b64encode(body)}.{_b64encode(sig)}"


def verify(
    token: str,
    *,
    expected_profile_id: int,
    expected_device_uuid: str,
    expected_source_sid: str,
) -> Optional[dict]:
    """Validate token and consume the nonce. Returns the payload dict
    on success, None on any failure path (don't leak which check
    failed — return value is identical for all rejection reasons).
    `expected_source_sid` is the session cookie of the redeeming
    request: the token was minted for that browser session and no
    other.
    """
    try:
        body_b64, sig_b64 = token.split(".", 1)
        body = _b64decode(body_b64)
        sig = _b64decode(sig_b64)
        expected_sig = hmac.new(_secret(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return None
        if (time.time() - int(payload.get("t") or 0)) > TOKEN_TTL_SECONDS:
            return None
        if int(payload.get("p") or 0) != int(expected_profile_id):
            return None
        if (payload.get("d") or "") != expected_device_uuid:
            return None
        if not expected_source_sid or (payload.get("s") or "") != expected_source_sid:
            return None
        nonce = payload.get("n") or ""
        if not nonce or nonce in _consumed:
            return None
        # Mark consumed BEFORE returning so a concurrent redemption of
        # the same nonce races into the rejected branch.
        _consumed[nonce] = time.time()
        if len(_consumed) > _CONSUMED_MAX:
            _consumed.popitem(last=False)
        return payload
    except Exception:  # noqa: BLE001
        return None
