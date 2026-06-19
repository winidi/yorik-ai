"""Reverse-proxy for the n8n editor with Yorik session as the auth gate.

Clicking the "Automations" tile on /r/home should drop the user straight
into n8n's editor with no second login. n8n's own user-management UI is
disabled (`N8N_USER_MANAGEMENT_DISABLED=true` in docker-compose); the
container trusts whoever can reach it on its loopback port. Yorik's
session cookie is the actual auth layer:

  Browser ── session cookie ──▶  Yorik FastAPI  ──▶  http://127.0.0.1:5678
                                  /n8n/* proxy
                                  (auth required)

Two consequences:
  1. n8n must NOT be published outside 127.0.0.1 (docker-compose
     enforces this — see the `ports: 127.0.0.1:5678:5678` line).
  2. The proxy needs to handle WebSocket too — n8n's editor uses
     Socket.IO at /rest/push for live execution updates. Without WS
     the editor still works for viewing/editing workflows but lacks
     the running-now indicator while a workflow executes.

Implementation: HTTP via httpx (same pattern as paperless_proxy.py),
WebSocket via the `websockets` library and a small bidirectional
shovel.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from .auth_sessions import current_user, get_user_for_session, COOKIE_NAME

log = logging.getLogger("yorik.n8n_proxy")

_UPSTREAM_BASE = os.getenv("N8N_INTERNAL_URL", "http://127.0.0.1:5678").rstrip("/")
_UPSTREAM_WS   = _UPSTREAM_BASE.replace("http://", "ws://").replace("https://", "wss://")

# Hop-by-hop request headers we never forward. n8n is a Node.js app and
# doesn't trust client-supplied auth headers, but stripping them is
# good hygiene anyway.
_HOP_BY_HOP_REQ = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding",
    "upgrade", "content-length",
    # Same reason as paperless_proxy.py — strip Accept-Encoding so
    # upstream sends uncompressed and the browser doesn't render
    # brotli/gzip bytes as text when we drop Content-Encoding on the
    # way out.
    "accept-encoding",
}

_HOP_BY_HOP_RES = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "content-encoding", "content-length",
}

router = APIRouter(prefix="/n8n", tags=["n8n"])

_client: httpx.AsyncClient | None = None


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=5.0),
            follow_redirects=False,
        )
    return _client


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy(path: str, request: Request, user: dict[str, Any] = Depends(current_user)) -> Response:
    # NOTE on N8N_PATH inconsistency:
    # With N8N_PATH=/n8n/, n8n generates URLs in its HTML/JS prefixed
    # with /n8n/ (so the browser routes them back through this proxy),
    # but the upstream server itself still serves under root paths
    # (/assets/, /rest/, /webhook/, …). So we STRIP /n8n/ on the way
    # in. The N8N_PATH setting is purely about URL generation — Yorik
    # owns the path namespacing.
    upstream_url = f"{_UPSTREAM_BASE}/{path}"
    qs = request.url.query
    if qs:
        upstream_url += f"?{qs}"

    fwd_headers: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _HOP_BY_HOP_REQ:
            continue
        fwd_headers[k] = v
    # Tell n8n it's behind a proxy so its URL builder produces the right
    # absolute URLs in webhook responses + redirects.
    fwd_headers["X-Forwarded-Proto"] = request.url.scheme
    fwd_headers["X-Forwarded-Host"]  = request.headers.get("host", "")
    fwd_headers["X-Forwarded-For"]   = request.client.host if request.client else ""

    body = await request.body()

    client = _client_get()
    try:
        upstream_resp = await client.request(
            request.method, upstream_url, headers=fwd_headers, content=body,
        )
    except httpx.ConnectError as exc:
        log.warning("n8n proxy: upstream unreachable: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="n8n is unreachable. Is the yorik-n8n container running?",
        )
    except httpx.RequestError as exc:
        log.warning("n8n proxy: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=f"n8n upstream error: {exc}")

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


# ─── WebSocket proxy ───────────────────────────────────────────────────────
#
# n8n's editor opens a Socket.IO connection at /rest/push to push live
# workflow-execution updates. Without proxying it, the editor still works
# for viewing/editing but feels static during runs. The proxy below
# requires the same session cookie as the HTTP path — anonymous
# WebSocket upgrades are rejected at the door.

@router.websocket("/{path:path}")
async def proxy_ws(websocket: WebSocket, path: str) -> None:
    # Manual session check — websocket connections don't go through the
    # global auth middleware (that one inspects HTTP only).
    sid = websocket.cookies.get(COOKIE_NAME)
    client_ip = websocket.client.host if websocket.client else None
    user = get_user_for_session(sid, ip=client_ip) if sid else None
    if not user:
        await websocket.close(code=4401)
        return

    qs = websocket.url.query
    # Same path-strip story as the HTTP proxy — n8n's WS endpoint lives
    # at /rest/push, not /n8n/rest/push.
    upstream_url = f"{_UPSTREAM_WS}/{path}" + (f"?{qs}" if qs else "")

    try:
        # Lazy import — websockets isn't strictly needed for HTTP-only
        # deployments, so don't fail at module import time if missing.
        import websockets
    except ImportError:
        log.warning("n8n WS proxy unavailable: `websockets` not installed")
        await websocket.close(code=1011)
        return

    await websocket.accept()

    try:
        async with websockets.connect(upstream_url, ping_interval=None) as upstream:
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
        log.warning("n8n WS proxy failed: %s", exc)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
