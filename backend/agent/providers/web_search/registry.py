# SPDX-License-Identifier: MIT
# Ported from NousResearch/hermes-agent agent/web_search_registry.py
# (MIT, https://github.com/NousResearch/hermes-agent). Adapted: config
# source changed from ``config.yaml`` (Hermes) to env vars (Yorik); the
# resolution algorithm (explicit-config → single-provider → legacy walk)
# is preserved bit-for-bit.
"""Web Search Provider Registry.

Central map of registered web providers. Populated by concrete provider
modules at import time via :func:`register_provider`; consumed by the
``web_search`` / ``web_extract`` / ``web_crawl`` tool wrappers (Phase 5)
to dispatch each call to the active backend.

Active selection
----------------
The active provider for a capability is chosen with this precedence:

1. ``YORIK_WEB_SEARCH_BACKEND`` / ``YORIK_WEB_EXTRACT_BACKEND`` /
   ``YORIK_WEB_CRAWL_BACKEND`` (per-capability override).
2. ``YORIK_WEB_BACKEND`` (shared fallback).
3. If exactly one registered provider supports the capability AND is
   available, use it.
4. Legacy preference order — ``firecrawl`` → ``parallel`` → ``tavily`` →
   ``exa`` → ``searxng`` → ``brave-free`` → ``ddgs`` — filtered by
   availability. Matches Hermes's historic order, so installs that don't
   set a config key keep landing on the same provider they would have
   with Hermes.
5. Otherwise None — the tool surfaces a helpful error.

The capability filter (``supports_search`` / ``supports_extract`` /
``supports_crawl``) is applied at every step so a search-only provider
configured as the extract backend correctly falls through.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, List, Optional

from .base import WebSearchProvider

logger = logging.getLogger("yorik.agent.web_search")


_providers: Dict[str, WebSearchProvider] = {}
_lock = threading.Lock()


def register_provider(provider: WebSearchProvider) -> None:
    """Register a web search / extract / crawl provider.

    Re-registration (same ``name``) overwrites the previous entry and logs
    at DEBUG — makes hot-reload scenarios (tests, dev loops) predictable.
    """
    if not isinstance(provider, WebSearchProvider):
        raise TypeError(
            f"register_provider() expects a WebSearchProvider instance, "
            f"got {type(provider).__name__}"
        )
    name = provider.name
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Web provider .name must be a non-empty string")
    with _lock:
        existing = _providers.get(name)
        _providers[name] = provider
    if existing is not None:
        logger.debug(
            "Web provider '%s' re-registered (was %r)",
            name, type(existing).__name__,
        )
    else:
        logger.debug(
            "Registered web provider '%s' (%s)",
            name, type(provider).__name__,
        )


def list_providers() -> List[WebSearchProvider]:
    """Return all registered providers, sorted by name."""
    with _lock:
        items = list(_providers.values())
    return sorted(items, key=lambda p: p.name)


def get_provider(name: str) -> Optional[WebSearchProvider]:
    """Return the provider registered under *name*, or None."""
    if not isinstance(name, str):
        return None
    with _lock:
        return _providers.get(name.strip())


# ---------------------------------------------------------------------------
# Active-provider resolution
# ---------------------------------------------------------------------------


def _read_env_backend(capability: str) -> Optional[str]:
    """Read the configured backend name for *capability* (search/extract/crawl).

    Precedence:
      - ``YORIK_WEB_{CAPABILITY}_BACKEND`` (per-capability)
      - ``YORIK_WEB_BACKEND`` (shared fallback)
    """
    per_cap = os.getenv(f"YORIK_WEB_{capability.upper()}_BACKEND")
    if isinstance(per_cap, str) and per_cap.strip():
        return per_cap.strip()
    shared = os.getenv("YORIK_WEB_BACKEND")
    if isinstance(shared, str) and shared.strip():
        return shared.strip()
    return None


_LEGACY_PREFERENCE = (
    "firecrawl",
    "parallel",
    "tavily",
    "exa",
    "searxng",
    "brave-free",
    "ddgs",
)


def _resolve(configured: Optional[str], *, capability: str) -> Optional[WebSearchProvider]:
    """Resolve the active provider for a capability.

    Rules (in order):
      1. Explicit config wins, ignoring availability — so the user gets a
         precise "X_API_KEY is not set" error instead of a silent backend
         switch.
      2. Single-provider shortcut when only one capable provider is
         registered AND ``is_available()``.
      3. Legacy preference walk, filtered by availability.
    Returns None when nothing matches.
    """
    with _lock:
        snapshot = dict(_providers)

    def _capable(p: WebSearchProvider) -> bool:
        if capability == "search":
            return bool(p.supports_search())
        if capability == "extract":
            return bool(p.supports_extract())
        if capability == "crawl":
            return bool(p.supports_crawl())
        return False

    def _is_available_safe(p: WebSearchProvider) -> bool:
        try:
            return bool(p.is_available())
        except Exception as exc:  # noqa: BLE001
            logger.debug("provider %s.is_available() raised %s", p.name, exc)
            return False

    # 1. Explicit config wins (even if not available — let downstream emit
    #    a precise error).
    if configured:
        provider = snapshot.get(configured)
        if provider is not None and _capable(provider):
            return provider
        if provider is None:
            logger.debug(
                "web backend '%s' configured but not registered; falling back",
                configured,
            )
        else:
            logger.debug(
                "web backend '%s' configured but does not support '%s'; falling back",
                configured, capability,
            )

    # 2 + 3. Fallback path — availability-filtered.
    eligible = [
        p for p in snapshot.values()
        if _capable(p) and _is_available_safe(p)
    ]
    if len(eligible) == 1:
        return eligible[0]

    for legacy in _LEGACY_PREFERENCE:
        provider = snapshot.get(legacy)
        if (
            provider is not None
            and _capable(provider)
            and _is_available_safe(provider)
        ):
            return provider

    return None


def get_active_search_provider() -> Optional[WebSearchProvider]:
    """Resolve the currently-active web search provider."""
    return _resolve(_read_env_backend("search"), capability="search")


def get_active_extract_provider() -> Optional[WebSearchProvider]:
    """Resolve the currently-active web extract provider."""
    return _resolve(_read_env_backend("extract"), capability="extract")


def get_active_crawl_provider() -> Optional[WebSearchProvider]:
    """Resolve the currently-active web crawl provider.

    Crawl is a niche capability — built-in providers that implement it
    are Tavily and Firecrawl. Callers should expect None and fall back
    when neither is configured.
    """
    return _resolve(_read_env_backend("crawl"), capability="crawl")


def _reset_for_tests() -> None:
    """Clear the registry. **Test-only.**"""
    with _lock:
        _providers.clear()


__all__ = [
    "register_provider",
    "list_providers",
    "get_provider",
    "get_active_search_provider",
    "get_active_extract_provider",
    "get_active_crawl_provider",
]
