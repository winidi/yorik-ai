"""DuckDuckGo (ddgs) web search provider — bundled default, no API key.

First concrete provider implementing :class:`WebSearchProvider`. Uses the
``ddgs`` package (formerly ``duckduckgo-search``) which is pure-Python +
optional ``primp`` HTTP backend.

Capability surface: search only. Extract/crawl require a different
backend (Tavily, Firecrawl, etc.) — see web_search/README.md.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import WebSearchProvider

logger = logging.getLogger("yorik.agent.web_search.ddgs")


class DDGSProvider(WebSearchProvider):
    """DuckDuckGo HTML scrape via the ``ddgs`` library.

    Free, no API key, rate-limited at DuckDuckGo's end (~10-15 req/min
    before they start serving CAPTCHAs).
    """

    @property
    def name(self) -> str:
        return "ddgs"

    @property
    def display_name(self) -> str:
        return "DuckDuckGo (free)"

    def is_available(self) -> bool:
        try:
            import ddgs  # noqa: F401
            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        if not query or not query.strip():
            return {"success": False, "error": "empty query"}
        try:
            from ddgs import DDGS
        except ImportError as exc:
            return {"success": False, "error": f"ddgs package not installed: {exc}"}

        try:
            with DDGS() as ddgs:
                raw = list(ddgs.text(query.strip(), max_results=max(1, min(int(limit), 25))))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ddgs search failed for %r: %s", query, exc)
            return {"success": False, "error": f"ddgs.text() failed: {type(exc).__name__}: {exc}"}

        web = []
        for i, r in enumerate(raw, start=1):
            web.append({
                "title":       r.get("title") or "",
                "url":         r.get("href") or r.get("url") or "",
                "description": r.get("body") or r.get("description") or "",
                "position":    i,
            })
        return {"success": True, "data": {"web": web}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name":  self.display_name,
            "badge": "free",
            "tag":   "DuckDuckGo HTML scrape — no API key, rate-limited at DDG's end.",
            "env_vars": [],
        }


__all__ = ["DDGSProvider"]
