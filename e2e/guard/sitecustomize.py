"""Network guard for the test household's server (loaded through
PYTHONPATH by e2e/household.py; never by the real Yorik).

The test Yorik may reach its own database, itself, the fake model and
the PDF renderer. Every other address on this machine, the home network
or the tailnet is refused, so a forgotten default ("the WhatsApp bridge
is on :3015") can never reach the household's real services. The public
internet stays open for model downloads. Refusals are written to
YORIK_E2E_BLOCKED_LOG so a run shows what the app tried to reach.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import time

_ALLOWED_LOCAL_PORTS = {int(p) for p in os.environ.get("YORIK_E2E_ALLOWED_PORTS", "").split(",") if p}
_LOG = os.environ.get("YORIK_E2E_BLOCKED_LOG")
_PRIVATE = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10", "169.254.0.0/16", "fc00::/7", "fe80::/10", "fd7a:115c:a1e0::/48",
)]


def _refused(address) -> bool:
    if not isinstance(address, tuple) or len(address) < 2:
        return False                       # unix sockets
    host, port = address[0], address[1]
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        ip = None
        if host in ("localhost",):
            ip = ipaddress.ip_address("127.0.0.1")
    if ip is None or not any(ip in net for net in _PRIVATE):
        return False
    return not (ip.is_loopback and int(port) in _ALLOWED_LOCAL_PORTS)


def _note(address) -> None:
    if _LOG:
        with open(_LOG, "a") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} refused {address[0]}:{address[1]}\n")


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def connect(self, address):
    if _refused(address):
        _note(address)
        raise ConnectionRefusedError(111, f"test household: {address[0]}:{address[1]} is off limits")
    return _connect(self, address)


def connect_ex(self, address):
    if _refused(address):
        _note(address)
        return 111
    return _connect_ex(self, address)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
