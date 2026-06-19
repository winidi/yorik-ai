"""WLAN-based trust gate for kiosk routes.

The kiosk experience (ambient screen, photo slideshow, switch-user
PIN pad, voice-on-tablet) is dangerous to expose off-LAN: a stranger
on the open internet who got hold of a session cookie would be able
to operate a household member's account through the kiosk surface,
bypassing the password layer.

This module is the single check that gates that surface. Callers ask
`is_trusted_lan_request(request)`; True means "the calling device is
on the household's local network and can be trusted with kiosk
capability." False means kiosk routes refuse with 403 even when the
session cookie is otherwise valid.

What counts as "trusted LAN":
  - Loopback (127.0.0.0/8, ::1) — always trusted, so local dev and
    server-side requests never trip the gate.
  - YORIK_TRUSTED_CIDRS — admin-supplied comma-separated CIDR list.
    Highest precedence, intended for production where the household's
    actual subnet is known (e.g. "192.168.1.0/24").
  - Auto-detected server /24 — when YORIK_TRUSTED_CIDRS is unset,
    we fall back to trusting the server's own primary private subnet.
    Convenient default for self-hosted installs where the YorikWall
    tablet is on the same LAN as the server.

What does NOT happen here:
  - We never look at the X-Forwarded-For chain. If you put Yorik
    behind a reverse proxy, configure the proxy to fail closed for
    off-LAN traffic and trust `request.client.host` from there.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from typing import List

from fastapi import Request

log = logging.getLogger("yorik.wlan_trust")

_ALWAYS_TRUSTED = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]

# RFC1918 + link-local + Tailscale CGNAT. The auto-detect default:
# trust anything that could plausibly be "on my network" for a
# self-hosted install. RFC1918 covers home/SMB wifi; the 100.64/10
# CGNAT block covers Tailscale tailnets (the typical "secure remote
# access to my Yorik" path). Anyone on a Tailscale 100.x address has
# already authenticated to the tailnet, so trusting the block here
# matches the user's mental model of "my devices, even when away."
# Admins who want a tighter set override via YORIK_TRUSTED_CIDRS.
_DEFAULT_PRIVATE = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # Tailscale (and other CGNAT)
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _parse_cidr_list(raw: str) -> List[ipaddress._BaseNetwork]:
    out: List[ipaddress._BaseNetwork] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("YORIK_TRUSTED_CIDRS: ignoring malformed entry %r", part)
    return out


def _build_trusted() -> List[ipaddress._BaseNetwork]:
    nets = list(_ALWAYS_TRUSTED)
    raw = os.getenv("YORIK_TRUSTED_CIDRS", "").strip()
    if raw:
        nets.extend(_parse_cidr_list(raw))
        log.info("wlan_trust: using YORIK_TRUSTED_CIDRS = %s (loopback always included)", raw)
    else:
        nets.extend(_DEFAULT_PRIVATE)
        log.info("wlan_trust: no YORIK_TRUSTED_CIDRS set — defaulting to RFC1918 + link-local + Tailscale CGNAT (100.64/10). Tighten by setting YORIK_TRUSTED_CIDRS in config.env.")
    return nets


_TRUSTED = _build_trusted()
_DENIED_ONCE: set = set()


def is_trusted_lan_request(request: Request) -> bool:
    """True when the request's client IP is inside the trusted set.

    Loopback always counts. Otherwise the trust set is either the
    YORIK_TRUSTED_CIDRS override (strict) or the RFC1918 default
    (permissive — every private block, on the theory that anything
    non-public is "on the house network" for a self-hosted install).

    Logs denied IPs once per IP at INFO level so a wall whose subnet
    isn't covered prints a single grep-able line in the server log:
    `wlan_trust: denied 192.168.4.50 path=/api/ambient/slideshow
    (trusted: 192.168.0.0/24, 127.0.0.0/8, ...)`."""
    client = request.client
    if client is None:
        return False
    try:
        ip = ipaddress.ip_address(client.host)
    except ValueError:
        return False
    if any(ip in net for net in _TRUSTED):
        return True
    key = str(ip)
    if key not in _DENIED_ONCE:
        _DENIED_ONCE.add(key)
        log.info(
            "wlan_trust: denied %s path=%s (trusted: %s)",
            key,
            request.url.path,
            ", ".join(str(n) for n in _TRUSTED),
        )
    return False


def trusted_cidrs_for_debug() -> List[str]:
    """For Settings → Devices to render the current trust config —
    admins shouldn't need to grep config.env to know what's allowed."""
    return [str(n) for n in _TRUSTED]
