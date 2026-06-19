# SPDX-License-Identifier: MIT
# Ported from NousResearch/hermes-agent agent/web_search_provider.py
# (MIT, https://github.com/NousResearch/hermes-agent). Adapted: docstring
# references to Hermes config paths (hermes_cli, plugins.enabled, etc.)
# replaced with Yorik equivalents; response-shape contract preserved
# bit-for-bit so Hermes-targeted provider implementations port cleanly.
"""Web Search Provider ABC.

Defines the pluggable-backend interface for web search and content extraction.
Concrete providers register instances via
``backend.agent.providers.web_search.registry.register_provider``; the
active one (selected via the ``YORIK_WEB_SEARCH_BACKEND`` env var or its
per-capability variants) services every ``web_search`` / ``web_extract``
tool call.

Phase-0 status: ABC + registry shipped, NO concrete backends yet. The
first bundled backend (``ddgs``, DuckDuckGo, no API key) lands in Phase
5 of the masterplan. Until then ``get_active_search_provider()`` returns
None and the dispatcher emits a "set up a web backend" message.

Response shape (preserved from Hermes's contract — implementations
should match exactly so the tool wrapper does not have to translate):

Search results::

    {
        "success": True,
        "data": {
            "web": [
                {"title": str, "url": str, "description": str, "position": int},
                ...
            ]
        }
    }

Extract results::

    {
        "success": True,
        "data": [
            {"url": str, "title": str, "content": str,
             "raw_content": str, "metadata": dict},
            ...
        ]
    }

On failure (either capability)::

    {"success": False, "error": str}
"""

from __future__ import annotations

import abc
from typing import Any, Dict, List


class WebSearchProvider(abc.ABC):
    """Abstract base class for a web search / extract / crawl backend.

    Subclasses must implement :meth:`is_available` and at least one of
    :meth:`search` / :meth:`extract` / :meth:`crawl`. The
    :meth:`supports_search` / :meth:`supports_extract` / :meth:`supports_crawl`
    capability flags let the registry route each tool call to the right
    provider, and let multi-capability providers (Firecrawl, Tavily, Exa,
    ...) advertise multiple capabilities from a single class.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used in ``YORIK_WEB_SEARCH_BACKEND`` /
        ``YORIK_WEB_EXTRACT_BACKEND`` / ``YORIK_WEB_BACKEND`` env vars.

        Lowercase, no spaces; hyphens permitted. Examples: ``brave-free``,
        ``ddgs``, ``searxng``, ``firecrawl``.
        """

    @property
    def display_name(self) -> str:
        """Human-readable label for picker UIs. Defaults to ``name``."""
        return self.name

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return True when this provider can service calls.

        Typically a cheap check (env var present, optional Python dep
        importable, instance URL set). Must NOT make network calls — this
        runs at tool-registration time and on every settings paint.
        """

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        """Override + implement :meth:`extract` to advertise this capability.

        Both sync and async :meth:`extract` implementations are valid — the
        dispatcher detects coroutine functions via
        :func:`inspect.iscoroutinefunction` and awaits as needed.
        """
        return False

    def supports_crawl(self) -> bool:
        """Override + implement :meth:`crawl` to advertise this capability.

        Crawl differs from extract: the agent provides a *seed URL* and
        the provider walks linked pages on its own — useful for
        documentation sites where the agent doesn't know all relevant
        URLs upfront.
        """
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a web search.

        Override when :meth:`supports_search` returns True. Callers should
        gate on :meth:`supports_search` before calling.
        """
        raise NotImplementedError(
            f"{self.name} does not support search (override supports_search)"
        )

    def extract(self, urls: List[str], **kwargs: Any) -> Any:
        """Extract content from one or more URLs.

        Override when :meth:`supports_extract` returns True.

        Return shape: list of dicts ::

            [
                {
                    "url": str,
                    "title": str,
                    "content": str,
                    "raw_content": str,
                    "metadata": dict,           # optional
                    "error": str,               # optional, only on per-URL failure
                },
                ...
            ]

        Implementations MAY be ``async def`` — the dispatcher detects
        coroutines and awaits.

        ``kwargs`` may carry forward-compat fields (``format``, ``include_raw``,
        ``max_chars``); implementations should ignore unknown keys.
        """
        raise NotImplementedError(
            f"{self.name} does not support extract (override supports_extract)"
        )

    def crawl(self, url: str, **kwargs: Any) -> Any:
        """Crawl a seed URL and return results.

        Override when :meth:`supports_crawl` returns True.

        Return shape: ``{"results": [{"url": str, "title": str,
        "content": str, ...}, ...]}``.

        Implementations MAY be ``async def``. ``kwargs`` may carry
        forward-compat fields (``max_depth``, ``include_domains``);
        implementations should ignore unknown keys.
        """
        raise NotImplementedError(
            f"{self.name} does not support crawl (override supports_crawl)"
        )

    def get_setup_schema(self) -> Dict[str, Any]:
        """Return provider metadata for the settings/picker UI.

        Shape::

            {
                "name": "Brave Search (Free)",
                "badge": "free",
                "tag": "No paid tier needed — uses Brave's free API.",
                "env_vars": [
                    {"key": "BRAVE_SEARCH_API_KEY",
                     "prompt": "Brave Search API key",
                     "url": "https://brave.com/search/api/"},
                ],
            }

        Default: minimal entry derived from ``display_name``.
        """
        return {
            "name": self.display_name,
            "badge": "",
            "tag": "",
            "env_vars": [],
        }


__all__ = ["WebSearchProvider"]
