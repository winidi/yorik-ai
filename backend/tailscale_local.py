"""What Yorik knows about its own Tailscale node, and the one Tailscale
API call it makes: a device-share invite for a person who joins.

Everything here is read-only on the box. Yorik never turns Serve or
Funnel on or off itself; the admin does that once (scripts/yorik
join-page, or the commands Settings → System shows).

- self_info():      this node's MagicDNS name and stable node id, from
                    `tailscale status --json` (no root needed)
- base_url():       where family phones reach Yorik over the tailnet,
                    e.g. https://workstation.tailnet.ts.net:8445
- join_page_url():  the public join page, if a Funnel serves it
- create_device_invite(): a single-use link that shares ONLY this
                    machine with the person who opens it. Needs a
                    Tailscale API access token or OAuth client, stored
                    encrypted under credential_store "tailscale_api".
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from typing import Any, Optional

import httpx

from . import credential_store

log = logging.getLogger("yorik.tailscale")

_API = "https://api.tailscale.com/api/v2"
_CACHE: dict[str, tuple[float, Any]] = {}
_TTL_S = 300
JOIN_PAGE_PORT = int(os.getenv("YORIK_JOIN_PAGE_PORT", "10000"))


_SOCKET = "/var/run/tailscale/tailscaled.sock"
# CLI command → tailscaled local API path, for the container install
# where only the socket is mounted (no tailscale binary inside).
_LOCALAPI = {("status", "--json"): "/localapi/v0/status",
             ("serve", "status", "--json"): "/localapi/v0/serve-config"}


def _localapi(args: tuple[str, ...]) -> Optional[dict[str, Any]]:
    path = _LOCALAPI.get(args)
    if not path or not os.path.exists(_SOCKET):
        return None
    try:
        transport = httpx.HTTPTransport(uds=_SOCKET)
        with httpx.Client(transport=transport, timeout=6) as c:
            r = c.get(f"http://local-tailscaled.sock{path}", headers={"Sec-Tailscale": "localapi"})
        return r.json() if r.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001
        log.info("tailscale local API %s failed: %s", path, exc)
        return None


def _run_json(*args: str) -> Optional[dict[str, Any]]:
    exe = shutil.which("tailscale")
    if not exe:
        return _localapi(tuple(args))
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info("tailscale %s failed: %s", " ".join(args), exc)
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout or "{}")
    except ValueError:
        return None


def _cached(key: str, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL_S:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.time(), val)
    return val


def self_info() -> Optional[dict[str, str]]:
    """{'dns_name': 'workstation.tailnet.ts.net', 'node_id': 'n…'} or None
    when Tailscale isn't installed, logged out, or not reachable."""
    def load():
        st = _run_json("status", "--json")
        me = (st or {}).get("Self") or {}
        dns = (me.get("DNSName") or "").rstrip(".")
        if not dns:
            return None
        return {"dns_name": dns, "node_id": me.get("ID") or ""}
    return _cached("self", load)


def base_url() -> Optional[str]:
    """How phones reach Yorik over the tailnet. YORIK_PUBLIC_URL wins;
    otherwise the MagicDNS name on the Serve HTTPS port."""
    override = (os.getenv("YORIK_PUBLIC_URL") or "").strip().rstrip("/")
    if override:
        return override
    me = self_info()
    if not me:
        return None
    port = (os.getenv("YORIK_TAILSCALE_HTTPS_PORT") or "8445").strip()
    return f"https://{me['dns_name']}" + ("" if port in ("", "443") else f":{port}")


def join_page_url() -> Optional[str]:
    """The public join page, when Funnel serves it on JOIN_PAGE_PORT.
    Read from `tailscale serve status --json`; YORIK_JOIN_PAGE_URL wins."""
    override = (os.getenv("YORIK_JOIN_PAGE_URL") or "").strip()
    if override:
        return override.rstrip("/") + "/"

    def load():
        me = self_info()
        cfg = _run_json("serve", "status", "--json")
        if not me or not cfg:
            return None
        host_port = f"{me['dns_name']}:{JOIN_PAGE_PORT}"
        if not (cfg.get("AllowFunnel") or {}).get(host_port):
            return None
        return f"https://{host_port}/"
    return _cached("join_page", load)


def serve_url_for_local_port(port: int) -> Optional[str]:
    """The tailnet HTTPS address Serve publishes a local service on, e.g.
    Immich on localhost:2283 → https://box.tailnet.ts.net:8443."""
    def load():
        cfg = _run_json("serve", "status", "--json") or {}
        for host_port, web in (cfg.get("Web") or {}).items():
            for handler in (web.get("Handlers") or {}).values():
                proxy = (handler or {}).get("Proxy") or ""
                if proxy.rstrip("/").endswith(f":{port}"):
                    host, _, p = host_port.rpartition(":")
                    return f"https://{host}" + ("" if p == "443" else f":{p}")
        return None
    return _cached(f"serve:{port}", load)


def forget_cache() -> None:
    _CACHE.clear()


# ── Tailscale API (device invites) ────────────────────────────────────

def api_configured() -> bool:
    c = credential_store.get("tailscale_api") or {}
    return bool(c.get("api_key") or (c.get("client_id") and c.get("client_secret")))


def _bearer() -> Optional[str]:
    c = credential_store.get("tailscale_api") or {}
    if c.get("api_key"):
        return c["api_key"]
    if c.get("client_id") and c.get("client_secret"):
        # OAuth client credentials never expire; API keys do (max 90 d).
        r = httpx.post(f"{_API}/oauth/token", timeout=15,
                       data={"client_id": c["client_id"], "client_secret": c["client_secret"],
                             "grant_type": "client_credentials"})
        r.raise_for_status()
        return r.json().get("access_token")
    return None


def create_device_invite() -> dict[str, Any]:
    """A single-use invite that shares this machine. Returns
    {'ok': True, 'url': …} or {'ok': False, 'reason': …}; never raises."""
    if not api_configured():
        return {"ok": False, "reason": "not_configured"}
    me = self_info()
    if not me or not me.get("node_id"):
        return {"ok": False, "reason": "tailscale_not_running"}
    try:
        token = _bearer()
        r = httpx.post(f"{_API}/device/{me['node_id']}/device-invites", timeout=15,
                       headers={"Authorization": f"Bearer {token}"},
                       json=[{"multiUse": False, "allowExitNode": False}])
        if r.status_code >= 400:
            log.warning("tailscale device invite: HTTP %s %s", r.status_code, r.text[:300])
            return {"ok": False, "reason": f"api_{r.status_code}"}
        data = r.json()
        item = data[0] if isinstance(data, list) and data else data
        url = (item or {}).get("inviteUrl") or (item or {}).get("invite_url")
        if not url:
            log.warning("tailscale device invite: no inviteUrl in %s", str(data)[:300])
            return {"ok": False, "reason": "no_url"}
        return {"ok": True, "url": url}
    except Exception as exc:  # noqa: BLE001
        log.warning("tailscale device invite failed: %s", exc)
        return {"ok": False, "reason": "error"}


def check_api() -> dict[str, Any]:
    """For the settings card: can Yorik talk to the Tailscale API?"""
    if not api_configured():
        return {"ok": False, "message": "No Tailscale access saved yet."}
    try:
        token = _bearer()
        r = httpx.get(f"{_API}/tailnet/-/devices", timeout=15,
                      headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            return {"ok": True, "message": "Yorik can create invites."}
        return {"ok": False, "message": f"Tailscale refused the access (HTTP {r.status_code}). Check the key or its scopes."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Couldn't reach Tailscale: {exc}"}
