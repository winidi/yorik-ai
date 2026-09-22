"""Yorik as an OpenID Connect provider — so the Immich login IS the
Yorik login.

Decided with Dirk 2026-09-22 ("ganz einfach"): the Photos app frames
Immich, and Immich had its own login form, so whoever was signed into
Immich in that browser saw that library, whatever Yorik thought.
Immich can hand its login to an OpenID Connect provider; this module
is that provider, just big enough for Immich (and anything else in
the house that speaks OIDC):

  GET  /.well-known/openid-configuration   discovery
  GET  /oidc/jwks                          the public key
  GET  /oidc/authorize                     session cookie → code (or the Yorik login first)
  POST /oidc/token                         code → id_token + access_token
  GET  /oidc/userinfo                      the person behind an access token

The signing key is an RSA key made on first use and kept in the
credential store. A client (Immich) is a record in the credential
store with its secret and the exact redirect URIs it may use —
`scripts/configure_immich_oauth.py` writes both sides. Codes live 60
seconds and once; access tokens an hour, in memory (a restart logs
nobody out of Immich — Immich keeps its own session after the login).

The issuer is the URL the browser and the client use for Yorik; it is
taken from the request (behind Tailscale serve that is the https URL)
unless YORIK_OIDC_ISSUER pins it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode, quote

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import auth_sessions as _auth
from . import credential_store

log = logging.getLogger("yorik.oidc")

router = APIRouter(tags=["oidc"])

CODE_TTL_S = 60
TOKEN_TTL_S = 3600
_KEY_STORE = "oidc_signing_key"
_CLIENT_STORE = "oidc_client_{client_id}"
KNOWN_CLIENTS = ("immich",)

_lock = threading.Lock()
_codes: Dict[str, Dict[str, Any]] = {}
_access: Dict[str, Dict[str, Any]] = {}
_key_cache: Optional[Dict[str, Any]] = None


# ── keys and clients ────────────────────────────────────────────────

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def signing_key() -> Dict[str, Any]:
    """{private (PEM str), public (PEM str), kid}. Made once, kept."""
    global _key_cache
    if _key_cache:
        return _key_cache
    with _lock:
        if _key_cache:
            return _key_cache
        stored = credential_store.get(_KEY_STORE) or {}
        if not stored.get("private"):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode("ascii")
            public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
            stored = {"private": private, "public": public, "kid": secrets.token_hex(8)}
            credential_store.put(_KEY_STORE, stored)
            log.info("oidc: made the signing key (kid %s)", stored["kid"])
        _key_cache = stored
        return stored


def _jwk() -> Dict[str, Any]:
    key = serialization.load_pem_public_key(signing_key()["public"].encode("ascii"))
    numbers = key.public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": signing_key()["kid"],
            "n": _b64url(n), "e": _b64url(e)}


def get_client(client_id: str) -> Optional[Dict[str, Any]]:
    if client_id not in KNOWN_CLIENTS:
        return None
    return credential_store.get(_CLIENT_STORE.format(client_id=client_id))


def ensure_client(client_id: str, redirect_uris: list[str], issuer_url: Optional[str] = None) -> Dict[str, Any]:
    """Create or update a client: keeps an existing secret, replaces the
    redirect URIs with the given exact list. `issuer_url` is the Yorik
    URL the client was told; the app's CSP lets its iframes navigate
    there (Immich sends the Photos iframe to it for the login)."""
    if client_id not in KNOWN_CLIENTS:
        raise ValueError(f"unknown client {client_id!r}")
    existing = get_client(client_id) or {}
    record = {"client_id": client_id,
              "client_secret": existing.get("client_secret") or secrets.token_urlsafe(32),
              "redirect_uris": [u.strip() for u in redirect_uris if u.strip()],
              "issuer_url": (issuer_url or existing.get("issuer_url") or "").rstrip("/")}
    credential_store.put(_CLIENT_STORE.format(client_id=client_id), record)
    return record


def client_issuer_origins() -> list[str]:
    """Origins of the issuer URLs the clients were configured with —
    for the CSP frame-src, so the Photos iframe may follow Immich's
    login redirect to Yorik even when the browser opened Yorik under
    another host (localhost vs. the Tailscale name)."""
    from urllib.parse import urlsplit
    out: list[str] = []
    for cid in KNOWN_CLIENTS:
        try:
            u = urlsplit((get_client(cid) or {}).get("issuer_url") or "")
        except Exception:  # noqa: BLE001
            continue
        if u.scheme and u.netloc:
            out.append(f"{u.scheme}://{u.netloc}")
    return out


# ── issuer ──────────────────────────────────────────────────────────

def issuer(request: Request) -> str:
    pinned = (os.getenv("YORIK_OIDC_ISSUER") or "").strip().rstrip("/")
    if pinned:
        return pinned
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}"


# ── routes ──────────────────────────────────────────────────────────

@router.get("/.well-known/openid-configuration")
def discovery(request: Request) -> Dict[str, Any]:
    iss = issuer(request)
    return {
        "issuer": iss,
        "authorization_endpoint": f"{iss}/oidc/authorize",
        "token_endpoint": f"{iss}/oidc/token",
        "userinfo_endpoint": f"{iss}/oidc/userinfo",
        "jwks_uri": f"{iss}/oidc/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "claims_supported": ["sub", "email", "email_verified", "name", "preferred_username", "given_name"],
    }


@router.get("/oidc/jwks")
def jwks() -> Dict[str, Any]:
    return {"keys": [_jwk()]}


def _error_redirect(redirect_uri: str, error: str, state: Optional[str], description: str = "") -> RedirectResponse:
    q = {"error": error}
    if description:
        q["error_description"] = description
    if state:
        q["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(q)}", status_code=302)


@router.get("/oidc/authorize")
def authorize(
    request: Request,
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    response_type: str = Query("code"),
    scope: str = Query("openid"),
    state: Optional[str] = Query(None),
    nonce: Optional[str] = Query(None),
    code_challenge: Optional[str] = Query(None),
    code_challenge_method: Optional[str] = Query(None),
):
    client = get_client(client_id)
    # An unknown client or a redirect URI that is not exactly one of
    # the client's may not even receive an error redirect.
    if not client or redirect_uri not in (client.get("redirect_uris") or []):
        raise HTTPException(400, "unknown client or redirect_uri")
    if response_type != "code":
        return _error_redirect(redirect_uri, "unsupported_response_type", state)
    if "openid" not in (scope or "").split():
        return _error_redirect(redirect_uri, "invalid_scope", state, "openid is required")

    sid = request.cookies.get(_auth.COOKIE_NAME)
    user = _auth.get_user_for_session(sid, ip=request.client.host if request.client else None) if sid else None
    if not user:
        # The Yorik login first, then straight back here. The app reads
        # `oidc_next` and returns to this URL once the session exists.
        here = str(request.url.path) + ("?" + str(request.url.query) if request.url.query else "")
        return RedirectResponse(f"/r/home?oidc_next={quote(here, safe='')}", status_code=302)
    if user.get("disabled"):
        return _error_redirect(redirect_uri, "access_denied", state, "this account is disabled")

    code = secrets.token_urlsafe(32)
    with _lock:
        now = time.time()
        for k in [k for k, v in _codes.items() if v["exp"] < now]:
            _codes.pop(k, None)
        _codes[code] = {"user_id": str(user["id"]), "client_id": client_id, "redirect_uri": redirect_uri,
                        "nonce": nonce, "scope": scope, "code_challenge": code_challenge,
                        "code_challenge_method": (code_challenge_method or "plain") if code_challenge else None,
                        "exp": now + CODE_TTL_S}
    log.info("oidc: %s signs in to %s", user.get("name"), client_id)
    q = {"code": code}
    if state:
        q["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(q)}", status_code=302)


def _client_auth(request: Request, client_id: Optional[str], client_secret: Optional[str]) -> Dict[str, Any]:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("basic "):
        try:
            raw = base64.b64decode(auth[6:]).decode("utf-8")
            client_id, client_secret = raw.split(":", 1)
        except Exception:  # noqa: BLE001
            raise HTTPException(401, "bad client authentication")
    client = get_client(client_id or "")
    if not client or not secrets.compare_digest(client.get("client_secret") or "", client_secret or ""):
        raise HTTPException(401, "bad client authentication")
    return client


def _person(user_id: str) -> Optional[Dict[str, Any]]:
    from .external_users import paperless_username_for
    u = _auth.get_user_by_id(user_id)
    if not u or u.get("disabled"):
        return None
    name = u.get("name") or ""
    return {"sub": str(u["id"]), "email": u.get("email") or "", "email_verified": True,
            "name": name, "given_name": name.split(" ")[0] if name else "",
            "preferred_username": paperless_username_for(u.get("email") or "", name)}


@router.post("/oidc/token")
def token(
    request: Request,
    grant_type: str = Form(...),
    code: str = Form(None),
    redirect_uri: str = Form(None),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
):
    client = _client_auth(request, client_id, client_secret)
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    with _lock:
        grant = _codes.pop(code or "", None)          # once
    if not grant or grant["exp"] < time.time() or grant["client_id"] != client["client_id"] \
            or grant["redirect_uri"] != redirect_uri:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if grant.get("code_challenge"):
        if not code_verifier:
            return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier missing"}, status_code=400)
        expected = grant["code_challenge"]
        got = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest()) \
            if grant["code_challenge_method"] == "S256" else code_verifier
        if not secrets.compare_digest(expected, got):
            return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier mismatch"}, status_code=400)
    person = _person(grant["user_id"])
    if not person:
        return JSONResponse({"error": "invalid_grant", "error_description": "account gone"}, status_code=400)

    now = int(time.time())
    claims = {"iss": issuer(request), "sub": person["sub"], "aud": client["client_id"],
              "iat": now, "exp": now + TOKEN_TTL_S, "auth_time": now, **person}
    if grant.get("nonce"):
        claims["nonce"] = grant["nonce"]
    key = signing_key()
    id_token = jwt.encode(claims, key["private"], algorithm="RS256", headers={"kid": key["kid"]})
    access_token = secrets.token_urlsafe(32)
    with _lock:
        for k in [k for k, v in _access.items() if v["exp"] < now]:
            _access.pop(k, None)
        _access[access_token] = {"user_id": person["sub"], "exp": now + TOKEN_TTL_S}
    return {"access_token": access_token, "token_type": "Bearer", "expires_in": TOKEN_TTL_S,
            "id_token": id_token, "scope": grant.get("scope") or "openid"}


def _bearer_person(request: Request) -> Dict[str, Any]:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "bearer token required")
    with _lock:
        rec = _access.get(auth[7:].strip())
    if not rec or rec["exp"] < time.time():
        raise HTTPException(401, "token invalid or expired")
    person = _person(rec["user_id"])
    if not person:
        raise HTTPException(401, "account gone")
    return person


@router.get("/oidc/userinfo")
def userinfo(request: Request) -> Dict[str, Any]:
    return _bearer_person(request)


@router.post("/oidc/userinfo")
def userinfo_post(request: Request) -> Dict[str, Any]:
    return _bearer_person(request)
