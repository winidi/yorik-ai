"""Reverse-proxy for the Paperless-ngx web UI with Remote-User SSO.

Goal: clicking "Open in Paperless" in the React Documents app should land
the user inside Paperless already logged in as their own Paperless account,
with no second password prompt.

How it works:
  1. The Paperless container is configured with `PAPERLESS_ENABLE_HTTP_REMOTE_USER=true`
     and `PAPERLESS_FORCE_SCRIPT_NAME=/paperless`. Paperless then trusts the
     `Remote-User` HTTP header on incoming requests AND generates all its
     own absolute URLs (links, redirects, static asset paths, OAuth callback
     URLs) prefixed with `/paperless` — so the SPA "thinks" it lives at
     `https://yorik.local/paperless/` and never references the raw :8010
     port from inside the browser.
  2. Yorik's FastAPI mounts this catch-all proxy on `/paperless/{path}`.
     Every request requires a Yorik session cookie, the proxy strips any
     client-supplied `Remote-User` header (so a malicious browser can't
     impersonate someone), and injects `Remote-User: <paperless_username>`
     derived from the authenticated Yorik user. Paperless's RemoteUserBackend
     then auto-logs the user in.
  3. The proxy forwards the upstream's `Set-Cookie` headers verbatim. Since
     they come back on the *Yorik* origin, the browser stores them under
     that origin — no cross-port cookie surgery needed.

WebSocket proxying — Paperless's live "processing…" counter at
/paperless/ws/status/ — is handled by the `proxy_ws` handler at the
bottom of this file. Same Remote-User SSO contract.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocketState

from .auth_sessions import current_user, current_user_optional, get_user_for_session, COOKIE_NAME

log = logging.getLogger("yorik.paperless_proxy")

# Upstream Paperless lives in docker-compose on host port 8010 (container
# :8000). Override with PAPERLESS_INTERNAL_URL if you move it.
_UPSTREAM_BASE = os.getenv("PAPERLESS_INTERNAL_URL", "http://127.0.0.1:8010").rstrip("/")
# WebSocket upstream — Paperless's live upload-progress channel runs at
# /paperless/ws/*. Same host, but ws:// scheme. The proxy converts the
# incoming /paperless/ws/<...> to a WS connection upstream.
_UPSTREAM_WS = _UPSTREAM_BASE.replace("http://", "ws://", 1).replace("https://", "wss://", 1)

# Headers we never forward upstream — hop-by-hop per RFC 7230 plus auth
# headers the client must not be allowed to set themselves.
_HOP_BY_HOP_REQ = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding",
    "upgrade", "content-length",
    # Anti-spoofing: a browser must never be able to inject these.
    "remote-user", "x-remote-user", "x-forwarded-user",
    # X-Forwarded-* headers: strip whatever's incoming so we re-set
    # them ourselves below. Otherwise a request arriving via
    # tailscale-serve already carries X-Forwarded-Host (set by ts),
    # the proxy loop copies it, the explicit assignment below appends
    # a second copy under a different case, httpx case-normalises and
    # comma-joins, and Paperless rejects the duplicated host header
    # with a Django DisallowedHost 400. Strip + re-set is also the
    # security-correct posture — a client must not be able to spoof
    # what host/scheme/IP Paperless thinks the request came from.
    "x-forwarded-host", "x-forwarded-proto", "x-forwarded-for",
    "x-forwarded-port", "x-forwarded-prefix",
    # Force upstream to send uncompressed bytes. Otherwise upstream
    # picks gzip/br based on the client's Accept-Encoding, but httpx
    # (without the optional `brotli` dep) won't auto-decompress, and
    # we strip Content-Encoding on the way out — net result is the
    # browser renders compressed bytes as text (the "binary garbage"
    # bug). Cheaper than installing brotli + dealing with edge cases.
    "accept-encoding",
}

# Response headers httpx already handled (decoded gzip) — sending them
# back unchanged would make the browser try to decode again and fail.
_HOP_BY_HOP_RES = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "content-encoding", "content-length",
}

router = APIRouter(prefix="/paperless", tags=["paperless"])


def _paperless_username(user: dict[str, Any]) -> str:
    """Derive the Paperless username for a Yorik user.

    Phase C T11: only `platform_admin` impersonates the Paperless
    superuser. Workspace admins (`role='admin'`) route through their
    own Paperless account — otherwise a workspace admin would see
    every workspace's documents through the proxy, defeating per-
    workspace Paperless isolation.

    For non-platform-admin Yorik users (workspace admin and below),
    fall back to the slug rule used in `external_users.provision_paperless`
    so a properly-provisioned Paperless account picks up. (They'll see
    a permission-less view until provisioning is done — that's the
    documented limitation.)
    """
    if (user.get("role") or "").lower() == "platform_admin":
        return os.getenv("PAPERLESS_ADMIN_USER", "admin")
    email = (user.get("email") or "").lower()
    base = email.split("@")[0] if email else (user.get("name") or "").lower()
    cleaned = "".join(c for c in base if c.isalnum() or c in ".-_")[:30]
    if not cleaned:
        # Shouldn't happen for a logged-in user, but don't crash the proxy.
        cleaned = f"yorik{user.get('id', 0)}"
    return cleaned


# One shared async client. Default timeout is generous because Paperless
# can be slow on large document lists; we don't want spurious 504s.
_client: httpx.AsyncClient | None = None


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=5.0),
            follow_redirects=False,  # let the browser see the redirect chain
        )
    return _client


# Paths under /paperless/* that browsers fetch WITHOUT credentials
# (manifest spec → no cookies sent by default) or that don't need
# per-user gating. We let these through with an anonymous Remote-User
# so the asset loads and the SPA doesn't dead-lock on its manifest.
# These are ALL bundled Paperless static files — no PII reachable.
def _is_public_paperless_path(path: str) -> bool:
    if path.startswith("static/"):
        return True
    if path.endswith(".webmanifest"):
        return True
    return False


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy(
    path: str,
    request: Request,
    user_opt: Optional[dict[str, Any]] = Depends(current_user_optional),
) -> Response:
    # Public assets bypass the auth gate so they load without cookies.
    # Dynamic Paperless endpoints still require a Yorik session.
    public = _is_public_paperless_path(path)
    if not user_opt and not public:
        return Response(content='{"detail":"not authenticated"}',
                        status_code=401, media_type="application/json")
    user = user_opt or {"name": "anonymous", "email": "", "role": "viewer"}

    upstream_url = f"{_UPSTREAM_BASE}/paperless/{path}"
    qs = request.url.query
    if qs:
        upstream_url += f"?{qs}"

    # Build forwarded headers.
    fwd_headers: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _HOP_BY_HOP_REQ:
            continue
        fwd_headers[k] = v
    fwd_headers["Remote-User"] = _paperless_username(user)
    # Help Paperless build correct absolute URLs in redirects.
    fwd_headers["X-Forwarded-Proto"] = request.url.scheme
    fwd_headers["X-Forwarded-Host"] = request.headers.get("host", "")
    fwd_headers["X-Forwarded-For"] = request.client.host if request.client else ""

    body = await request.body()

    client = _client_get()
    import time as _time
    upstream_started = _time.monotonic()
    try:
        upstream_resp = await client.request(
            request.method,
            upstream_url,
            headers=fwd_headers,
            content=body,
        )
    except httpx.ConnectError as exc:
        dur = int((_time.monotonic() - upstream_started) * 1000)
        log.warning("paperless proxy: upstream unreachable (%dms): %s", dur, exc,
                    extra={"upstream": "paperless", "method": request.method,
                           "path": path, "duration_ms": dur, "status": "connect_error"})
        raise HTTPException(
            status_code=502,
            detail="Paperless is unreachable. Is the paperless-web container running?",
        )
    except httpx.RequestError as exc:
        dur = int((_time.monotonic() - upstream_started) * 1000)
        log.warning("paperless proxy: %s (%dms): %s", type(exc).__name__, dur, exc,
                    extra={"upstream": "paperless", "method": request.method,
                           "path": path, "duration_ms": dur, "status": "request_error",
                           "error_class": type(exc).__name__})
        raise HTTPException(status_code=502, detail=f"Paperless upstream error: {exc}")

    dur = int((_time.monotonic() - upstream_started) * 1000)
    # Non-2xx upstream responses are interesting (today's thumb 400!) —
    # log at WARNING so they're greppable. 2xx at INFO is the heartbeat.
    _lvl = logging.WARNING if upstream_resp.status_code >= 400 else logging.INFO
    log.log(_lvl, "paperless %s /%s -> %d (%dms)",
            request.method, path, upstream_resp.status_code, dur,
            extra={"upstream": "paperless", "method": request.method,
                   "path": path, "upstream_status": upstream_resp.status_code,
                   "duration_ms": dur,
                   "status": "ok" if upstream_resp.status_code < 400 else "upstream_error"})

    out_headers: list[tuple[str, str]] = []
    for k, v in upstream_resp.headers.multi_items():
        if k.lower() in _HOP_BY_HOP_RES:
            continue
        out_headers.append((k, v))

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=dict(out_headers),
        media_type=upstream_resp.headers.get("content-type"),
    )


# ── WebSocket proxy for Paperless live updates ────────────────────────
#
# Paperless's web UI opens ws://.../paperless/ws/status/ during file
# ingestion to push "your upload is processing… N% done" updates. Without
# this proxy, the channel fails silently — uploads still work, but the
# UI shows no live progress.
#
# Same pattern as backend/n8n_proxy.py:
#   1. Manual session check (WS bypasses the HTTP auth middleware)
#   2. Open an upstream WS to the Paperless container at the same path
#   3. Bidirectional forwarder until either side closes

@router.websocket("/ws/{path:path}")
async def proxy_ws(websocket: WebSocket, path: str) -> None:
    sid = websocket.cookies.get(COOKIE_NAME)
    client_ip = websocket.client.host if websocket.client else None
    user = get_user_for_session(sid, ip=client_ip) if sid else None
    if not user:
        await websocket.close(code=4401)
        return

    qs = websocket.url.query
    upstream_url = f"{_UPSTREAM_WS}/paperless/ws/{path}" + (f"?{qs}" if qs else "")

    try:
        import websockets
    except ImportError:
        log.warning("paperless WS proxy unavailable: `websockets` not installed")
        await websocket.close(code=1011)
        return

    await websocket.accept()

    try:
        # Paperless's Django Channels expects a Remote-User header on
        # the WS upgrade (same SSO contract as the HTTP side).
        upstream_headers = [("Remote-User", _paperless_username(user))]
        async with websockets.connect(
            upstream_url,
            ping_interval=None,
            additional_headers=upstream_headers,
        ) as upstream:
            async def client_to_upstream() -> None:
                try:
                    while websocket.client_state == WebSocketState.CONNECTED:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        if "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    return

            async def upstream_to_client() -> None:
                try:
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except websockets.exceptions.ConnectionClosed:
                    return

            done, pending = await asyncio.wait(
                {asyncio.create_task(client_to_upstream()),
                 asyncio.create_task(upstream_to_client())},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except Exception as exc:  # noqa: BLE001
        log.warning("paperless WS proxy failed: %s", exc)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
