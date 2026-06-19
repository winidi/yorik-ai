"""SSRF guard tests for the trafilatura web-extract provider.

Pins the policy at
backend/agent/providers/web_search/trafilatura.py:_is_private_or_local()
— see THREAT_MODEL.md row "SSRF via web_extract" for the threat we
mitigate. New extract providers (Tavily, Firecrawl, …) MUST replicate
this guard.
"""

from __future__ import annotations

import socket

import pytest


@pytest.fixture
def _is_private_or_local():
    from backend.agent.providers.web_search.trafilatura import _is_private_or_local
    return _is_private_or_local


@pytest.fixture
def _fetch_one():
    from backend.agent.providers.web_search.trafilatura import _fetch_one
    return _fetch_one


@pytest.mark.parametrize("host", [
    "localhost",
    "0.0.0.0",
    "::1",
    "::",
    "anything.localhost",
    "router.local",
    "server.internal",
])
def test_well_known_local_hostnames_refused(_is_private_or_local, host):
    assert _is_private_or_local(host) is True


@pytest.mark.parametrize("host", [
    "127.0.0.1",
    "127.55.42.1",     # rest of 127.0.0.0/8
    "10.0.0.5",         # private RFC1918
    "10.255.255.254",
    "192.168.1.1",
    "192.168.99.99",
    "172.16.0.1",       # private RFC1918
    "172.31.255.254",
    "169.254.169.254",  # AWS / GCP / Azure metadata
])
def test_private_and_metadata_ips_refused(_is_private_or_local, monkeypatch, host):
    """Stub getaddrinfo so DNS doesn't matter — we feed the IP directly
    and check the IP-classification branch."""
    def fake_resolve(h, _port):
        return [(socket.AF_INET, 0, 0, "", (host, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_resolve)
    assert _is_private_or_local(host) is True


def test_public_ip_allowed(_is_private_or_local, monkeypatch):
    """8.8.8.8 (public DNS) must NOT be refused — otherwise the
    guard is broken in the deny-by-default direction."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p: [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))])
    assert _is_private_or_local("dns.google") is False


def test_unresolvable_host_not_treated_as_private(_is_private_or_local, monkeypatch):
    """DNS failure → unknown → don't refuse based on the guard. (The
    fetch will then fail naturally at the requests.get() level.) This
    matches the implementation's choice to fail open on gaierror."""
    def fail(h, p):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert _is_private_or_local("does-not-resolve.example") is False


def test_fetch_one_rejects_file_scheme(_fetch_one):
    out = _fetch_one("file:///etc/passwd")
    assert out["content"] == ""
    assert "refused" in (out.get("error") or "")
    assert "scheme" in (out.get("error") or "")


def test_fetch_one_rejects_javascript_scheme(_fetch_one):
    out = _fetch_one("javascript:alert(1)")
    assert "refused" in (out.get("error") or "")


def test_fetch_one_rejects_localhost(_fetch_one):
    out = _fetch_one("http://localhost:8000/admin")
    assert "private/loopback" in (out.get("error") or "")


def test_fetch_one_rejects_link_local(_fetch_one, monkeypatch):
    """169.254.169.254 (cloud metadata) — same refusal."""
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, p: [(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))])
    out = _fetch_one("http://metadata.example/latest/meta-data/")
    assert "private/loopback" in (out.get("error") or "")


def test_fetch_one_rejects_missing_host(_fetch_one):
    out = _fetch_one("http:///")
    assert "refused" in (out.get("error") or "")
