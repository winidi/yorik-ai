"""Trafilatura web-extract provider — bundled default for `web_extract`.

The pre-existing ddgs provider only does search. This adds extract-only
support using `trafilatura` (https://trafilatura.readthedocs.io/) for
main-text extraction. Zero API keys, pure-Python, German-friendly.

Security model (matches the policy in the WebExtractTool docstring):

  - SSRF guard: only http(s) schemes; refuses localhost / private IPs /
    link-local / multicast / reserved ranges. DNS-rebinding mitigation
    by re-resolving each host before fetching.
  - robots.txt respected (1h-cached per host).
  - Polite User-Agent identifying Yorik.
  - Hard caps: 5 MB raw body, 16 KB extracted text per page, 15 s timeout.

Capability surface: extract-only. Search lives in ddgs (already
registered); a separate trafilatura.search() doesn't make sense.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from typing import Any, Dict, List, Optional
from urllib import robotparser
from urllib.parse import urlparse

import requests

from .base import WebSearchProvider

logger = logging.getLogger("yorik.agent.web_search.trafilatura")

USER_AGENT = "Yorik/0.3 (https://github.com/winidi/yorik-ai; personal-OS-fetcher)"
TIMEOUT_S = 15
MAX_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 16000

# 1h robots.txt cache.
_ROBOTS_CACHE: dict[str, tuple[float, robotparser.RobotFileParser]] = {}
_ROBOTS_TTL_S = 3600


def _is_private_or_local(host: str) -> bool:
    """SSRF guard — refuse anything that resolves to a private network
    or special-use IP. DNS-rebinding-safe (resolves on every call)."""
    h = (host or "").strip().lower()
    if not h or h in {"localhost", "0.0.0.0", "::1", "::"}:
        return True
    if h.endswith(".localhost") or h.endswith(".local") or h.endswith(".internal"):
        return True
    try:
        infos = socket.getaddrinfo(h, None)
    except socket.gaierror:
        return False
    for fam, _, _, _, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
            return True
    return False


def _robots_allows(url: str) -> bool:
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return False
    key = f"{p.scheme}://{p.netloc}"
    now = time.time()
    cached = _ROBOTS_CACHE.get(key)
    if cached and (now - cached[0]) < _ROBOTS_TTL_S:
        rp = cached[1]
    else:
        rp = robotparser.RobotFileParser()
        rp.set_url(f"{key}/robots.txt")
        try:
            rp.read()
        except Exception:
            _ROBOTS_CACHE[key] = (now, rp)
            return True  # treat missing/unreachable robots.txt as allow
        _ROBOTS_CACHE[key] = (now, rp)
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def _fetch_one(url: str) -> Dict[str, Any]:
    """Single-URL fetch + extract. Returns the per-URL result dict in
    the contract shape expected by WebSearchProvider.extract."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"url": url, "title": "", "content": "",
                "error": f"refused: unsupported scheme {parsed.scheme!r}"}
    if not parsed.netloc:
        return {"url": url, "title": "", "content": "", "error": "refused: missing host"}
    if _is_private_or_local(parsed.hostname or parsed.netloc):
        return {"url": url, "title": "", "content": "",
                "error": "refused: private/loopback address (SSRF guard)"}
    if not _robots_allows(url):
        return {"url": url, "title": "", "content": "",
                "error": f"refused: {parsed.netloc}/robots.txt disallows Yorik"}

    try:
        with requests.get(
            url,
            headers={
                "User-Agent":      USER_AGENT,
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9",
                "Accept-Language": "de,en;q=0.7",
            },
            timeout=TIMEOUT_S,
            stream=True,
        ) as r:
            r.raise_for_status()
            content_type = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if content_type and "html" not in content_type and "xml" not in content_type:
                return {"url": url, "title": "", "content": "",
                        "error": f"refused: non-HTML content-type ({content_type})"}
            raw = b""
            for chunk in r.iter_content(chunk_size=64 * 1024):
                raw += chunk
                if len(raw) > MAX_BYTES:
                    return {"url": url, "title": "", "content": "",
                            "error": f"refused: page exceeds {MAX_BYTES} bytes"}
            final_url = r.url
    except requests.RequestException as exc:
        return {"url": url, "title": "", "content": "",
                "error": f"fetch failed: {type(exc).__name__}: {exc}"}

    # Main-text extraction via trafilatura. Falls back to a naive tag
    # strip when trafilatura returns nothing (paywalls / JS-only).
    text = ""
    try:
        import trafilatura
        text = trafilatura.extract(
            raw.decode("utf-8", errors="replace"),
            include_links=False, include_images=False,
            include_tables=True, favor_recall=True,
        ) or ""
    except Exception:
        text = ""
    if not text:
        import re
        decoded = raw.decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", decoded)
        text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n…[truncated]"

    import re as _re
    title_m = _re.search(
        r"<title[^>]*>([^<]+)</title>",
        raw.decode("utf-8", errors="replace"), _re.IGNORECASE,
    )
    title = (title_m.group(1).strip() if title_m else "") or final_url

    return {
        "url":         final_url,
        "title":       title,
        "content":     text,
        "raw_content": text,
        "metadata":    {"bytes": len(raw), "extractor": "trafilatura"},
    }


class TrafilaturaProvider(WebSearchProvider):
    """Pure-Python web-extract via trafilatura. Search-not-supported —
    the existing ddgs provider handles that."""

    @property
    def name(self) -> str:
        return "trafilatura"

    @property
    def display_name(self) -> str:
        return "Trafilatura (free, self-hosted)"

    def is_available(self) -> bool:
        try:
            import trafilatura  # noqa: F401
            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def extract(self, urls: List[str], **kwargs: Any) -> Dict[str, Any]:
        """Run _fetch_one over each URL. Sequential (not async); fine
        for the typical N=1-3 use case. Returns the contract shape
        {success: True, data: [...]}."""
        results = []
        for u in urls:
            try:
                results.append(_fetch_one(str(u).strip()))
            except Exception as exc:  # noqa: BLE001
                logger.warning("trafilatura extract of %s raised: %s", u, exc)
                results.append({
                    "url": u, "title": "", "content": "",
                    "error": f"crashed: {type(exc).__name__}: {exc}",
                })
        return {"success": True, "data": results}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name":  self.display_name,
            "badge": "free",
            "tag":   "Pure-Python main-text extraction. No API key, "
                     "SSRF-guarded, robots.txt-respecting. German-friendly.",
            "env_vars": [],
        }


__all__ = ["TrafilaturaProvider"]
