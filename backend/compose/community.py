"""Community-template catalogue fetcher.

Aligns with the yorik-community repo layout:
  catalogue.json          ← slim, machine-built by scripts/build_catalogue.py
  templates/<id>/manifest.json
  templates/<id>/<entry>.json   ← the actual template body

Fetch flow:
  1. GET <base>/catalogue.json — already-slim list of manifests.
  2. On install: GET <base>/templates/<id>/<entry> — the full body the
     local validator + writer expects.

This means the catalogue page-load is one cheap fetch, and the per-
template body is only pulled when the user actually installs it.

Auth: when the repo is private, set YORIK_COMMUNITY_GITHUB_TOKEN
(a fine-grained PAT with Contents:Read on the repo). The token is
sent as a Bearer header on every raw.githubusercontent.com request.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("yorik.compose.community")

# Default points at the yorik-community repo's root catalogue.json.
# Override the FULL URL via env to mirror or change branch.
DEFAULT_CATALOGUE_URL = (
    "https://raw.githubusercontent.com/winidi/yorik-community/main/"
    "catalogue.json"
)

CACHE_TTL_S = 300        # 5 min — catalogue rarely changes intra-session
FETCH_TIMEOUT_S = 8.0


_cache: Dict[str, Any] = {"at": 0.0, "url": None, "data": None}


def catalogue_url() -> str:
    return os.getenv("YORIK_COMMUNITY_TEMPLATES_URL", DEFAULT_CATALOGUE_URL).strip()


def fetch_catalogue(force: bool = False) -> Dict[str, Any]:
    """Return {templates: [...], source: url, fetched_at: iso, cached:
    bool, error: str|None}. Always returns a dict; on failure
    `templates` is empty and `error` carries a short reason so the UI
    can show a "couldn't reach community repo" banner."""
    url = catalogue_url()
    now = time.time()
    if (not force and _cache["url"] == url
            and _cache["data"] is not None
            and now - _cache["at"] < CACHE_TTL_S):
        d = dict(_cache["data"])
        d["cached"] = True
        return d

    try:
        # Allow a `file://` local override for testing without a network.
        if url.startswith("file://"):
            from pathlib import Path
            body = Path(url[len("file://"):]).read_text(encoding="utf-8")
            import json as _json
            data = _json.loads(body)
        else:
            # GitHub PAT support: when the catalogue lives in a private
            # repo, raw.githubusercontent.com requires auth. Read the
            # token from YORIK_COMMUNITY_GITHUB_TOKEN (works on both
            # raw.* and api.github.com URLs). For public repos this is
            # a no-op.
            headers = {"User-Agent": "yorik-os/1.0", "Accept": "application/json"}
            token = os.getenv("YORIK_COMMUNITY_GITHUB_TOKEN", "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            r = requests.get(url, timeout=FETCH_TIMEOUT_S, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("community catalogue fetch failed: %s", exc)
        out = {"templates": [], "source": url, "fetched_at": None,
               "cached": False, "error": str(exc)[:200]}
        # Don't poison the cache with the failure — next call retries.
        return out

    templates = data.get("templates") or []
    public_list = [_slim_for_listing(t) for t in templates]
    cached = {
        "templates":  public_list,
        "source":     url,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "cached":     False,
        "error":      None,
        # Manifests kept server-side so install can derive the entry
        # filename without a second catalogue fetch. Never returned
        # to the client.
        "_manifest_by_id": {t.get("id"): t for t in templates if t.get("id")},
    }
    _cache["at"] = now
    _cache["url"] = url
    _cache["data"] = cached
    return _public_view(cached)


def _slim_for_listing(t: Dict[str, Any]) -> Dict[str, Any]:
    """Listing rows — everything the UI shows on the picker, minus
    `content` (which can be several KB per template)."""
    return {
        "id":          t.get("id"),
        "name":        t.get("name"),
        "description": t.get("description") or "",
        "tags":        list(t.get("tags") or []),
        "vertical":    t.get("vertical"),
        "author":      t.get("author") or "community",
        "version":     t.get("version") or "1.0",
        "needs_apps":  list(t.get("needs_apps") or []),
        # Country codes (ISO 3166-1 alpha-2) or ["*"] for universal.
        # The modal's filter chip uses this to scope to one country.
        # Absent = treated as universal so old manifests don't get
        # hidden when filtering.
        "countries":   list(t.get("countries") or []),
        "locale":      t.get("locale"),
        "category":    t.get("category"),
    }


def _public_view(cached: Dict[str, Any]) -> Dict[str, Any]:
    """Mask the internal `_full_by_id` map before sending to the API."""
    return {k: v for k, v in cached.items() if not k.startswith("_")}


def get_full_template(template_id: str) -> Optional[Dict[str, Any]]:
    """Pull the full template JSON body for an id. The catalogue only
    carries slim manifests, so we resolve {manifest.entry} against
    {base}/templates/{id}/{entry} and fetch the body separately."""
    if (_cache["data"] is None
            or _cache["url"] != catalogue_url()):
        fetch_catalogue()
    manifests = (_cache["data"] or {}).get("_manifest_by_id") or {}
    manifest = manifests.get(template_id)
    if not manifest:
        return None

    # Old-style fallback: if the manifest inlined the full template
    # under `content`, just use it (handy for tests / single-file mode).
    if isinstance(manifest.get("content"), dict):
        return manifest["content"]

    # Derive the body URL: strip the catalogue filename from the URL
    # to get the repo base, then templates/<id>/<entry>.
    cat_url = catalogue_url()
    if "/" not in cat_url:
        return None
    base = cat_url.rsplit("/", 1)[0]
    entry = manifest.get("entry")
    if not entry:
        log.warning("community manifest %s has no `entry` field", template_id)
        return None
    body_url = f"{base}/templates/{template_id}/{entry}"

    try:
        if body_url.startswith("file://"):
            from pathlib import Path
            import json as _json
            text = Path(body_url[len("file://"):]).read_text(encoding="utf-8")
            return _json.loads(text)
        headers = {"User-Agent": "yorik-os/1.0", "Accept": "application/json"}
        token = os.getenv("YORIK_COMMUNITY_GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = requests.get(body_url, timeout=FETCH_TIMEOUT_S, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("community body fetch failed for %s: %s", template_id, exc)
        return None
