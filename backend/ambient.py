"""Kiosk / ambient-mode helpers.

The ambient route on the frontend (a wall-mounted tablet) needs three
pieces of data that don't fit any existing endpoint:

  1. A list of photos to slideshow (from ONE Immich album the admin
     configured for this device — see migration 048's kiosk_album_id).
  2. An "idle bundle" — next event, pending task count, etc. — so the
     idle overlay can render without N round-trips.
  3. The catalogue of Immich albums (for the Settings → Devices
     dropdown when the admin picks WHICH album to slideshow).

This module owns those three reads. It deliberately does NOT own auth
or route shape — main.py wires the FastAPI endpoints and enforces
kiosk-session-only / admin-only guards. Keeping the read logic
separate so it stays testable + composable (e.g. a future
@app.get("/api/ambient/preview") admin endpoint can reuse the same
fetcher).

All Immich access goes through credential_store.get("immich")'s admin
API key, same pattern immich_provisioning.py already uses. The kiosk
session is BOUND to a Yorik user (the device-owner, typically the
admin who set up the tablet) but the slideshow scope is the named
album only — see yorik-kiosk-plan.md "ambient photo scope" for the
privacy model.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import requests

from . import credential_store

log = logging.getLogger("yorik.ambient")

_TIMEOUT = 8

# CLIP blocklist cache: maps (user_id, phrases_key) → (expires_at, ids).
# Smart search per phrase is a ~200-500ms round-trip into Immich's ML
# service, and the kiosk refetches its slideshow every 5min — without
# caching we'd burn that latency 12× an hour for an answer that
# doesn't change unless the admin updates the phrases or new photos
# land. 10-minute TTL keeps things responsive enough that newly-added
# photos get re-evaluated soon-ish without hammering the model.
_BLOCKLIST_TTL_S       = 600
_BLOCKLIST_SIZE_PER_Q  = 100   # top-K results per phrase to fetch
_blocklist_cache: dict[tuple[int, str], tuple[float, frozenset[str]]] = {}
_blocklist_lock = threading.Lock()


def _immich_admin() -> Optional[dict[str, str]]:
    """The connector-level admin API key. Used for read-only catalog
    work (listing albums for the Settings dropdown) where the admin's
    cross-user view is the right scope. NEVER used to fetch slideshow
    photos — those come from the bound user's library via _immich_user
    below, so the kiosk-owner's photos populate the wall."""
    creds = credential_store.get("immich") or {}
    base = (creds.get("base_url") or "").rstrip("/")
    key = creds.get("api_key") or ""
    if not (base and key):
        return None
    return {"base_url": base, "api_key": key}


def _immich_user(user_id: str) -> Optional[dict[str, str]]:
    """Per-user Immich credentials for the kiosk-bound user. Slideshow
    queries (album fetch, today's photos) MUST use these — the admin
    key sees zero photos because the admin doesn't own any, so a
    kiosk built on the admin key would always render an empty wall.
    Falls back to the admin key when per-user isn't provisioned, so
    single-user installs still work (the admin IS the user there)."""
    from . import external_users
    creds = external_users.get_user_immich_creds(user_id)
    if creds and creds.get("api_key"):
        return {"base_url": (creds.get("base_url") or "").rstrip("/"),
                "api_key":  creds["api_key"]}
    return _immich_admin()


def _headers(s) -> dict[str, str]:
    return {"x-api-key": s["api_key"], "Accept": "application/json"}


def list_albums(*, user_id: Optional[int] = None) -> list[dict[str, Any]]:
    """All albums visible to the kiosk-bound user (or admin if no
    user passed). Returns [{id, name, asset_count, shared}] sorted
    by name. Empty list when Immich isn't configured — the Settings
    dropdown shows a "configure Immich first" link in that case.

    Per-user scope by default so the dropdown only shows albums the
    user can actually fetch from later — admin-scope would surface
    albums they have no permission on, leading to a silent empty
    slideshow when the admin picks one for them."""
    s = _immich_user(user_id) if user_id else _immich_admin()
    if not s:
        return []
    try:
        r = requests.get(f"{s['base_url']}/api/albums",
                         headers=_headers(s), timeout=_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("list_albums: %s", exc)
        return []
    if r.status_code != 200:
        log.warning("list_albums: status %s", r.status_code)
        return []
    out: list[dict[str, Any]] = []
    for a in r.json() or []:
        out.append({
            "id":          a.get("id") or "",
            "name":        a.get("albumName") or "",
            "asset_count": int(a.get("assetCount") or 0),
            # Albums Yorik manages for shared spaces have specific
            # auto-managed names; expose `shared` so the UI can flag
            # them ("this album is auto-synced by Yorik — be aware
            # changes here can be overwritten").
            "shared":      bool(a.get("shared")),
        })
    out.sort(key=lambda x: (x["name"] or "").lower())
    return out


def get_blocklist(user_id: str, phrases: list[str]) -> frozenset[str]:
    """Return the set of Immich asset IDs to hide from the slideshow,
    based on the admin's free-text filter phrases.

    Each phrase goes into Immich's CLIP-backed smart search; the union
    of top-K matches across all phrases is the blocklist. Cached per
    (user_id, phrases) for _BLOCKLIST_TTL_S so the kiosk's 5-minute
    poll doesn't fire 1+phrase ML calls every refresh.

    Empty phrases / Immich-not-configured / network errors all degrade
    to "nothing blocked" — the wall keeps rendering rather than 500ing.
    """
    if not phrases:
        return frozenset()
    norm = sorted({(p or "").strip() for p in phrases if (p or "").strip()})
    if not norm:
        return frozenset()
    key = (user_id, "\x1f".join(norm))
    now = time.time()

    with _blocklist_lock:
        hit = _blocklist_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

    s = _immich_user(user_id)
    if not s:
        return frozenset()

    blocked: set[str] = set()
    for q in norm:
        try:
            r = requests.post(
                f"{s['base_url']}/api/search/smart",
                json={"query": q, "size": _BLOCKLIST_SIZE_PER_Q},
                headers={**_headers(s), "Content-Type": "application/json"},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("get_blocklist %r: %s", q, exc)
            continue
        if r.status_code != 200:
            log.warning("get_blocklist %r: status %s body=%s",
                        q, r.status_code, r.text[:160])
            continue
        items = ((r.json() or {}).get("assets") or {}).get("items") or []
        for a in items:
            aid = a.get("id")
            if aid:
                blocked.add(aid)

    frozen = frozenset(blocked)
    with _blocklist_lock:
        _blocklist_cache[key] = (now + _BLOCKLIST_TTL_S, frozen)
    log.info("ambient blocklist: user=%s phrases=%d → %d blocked assets",
             user_id, len(norm), len(frozen))
    return frozen


def get_today_photos(
    user_id: str,
    *,
    limit: int = 50,
    exclude_ids: Optional[frozenset[str]] = None,
) -> list[dict[str, Any]]:
    """Photos taken in the bound user's library since local midnight.

    Powers the "show today's photos" opt-in toggle on the kiosk. Calls
    Immich's metadata-search endpoint with a takenAfter cutoff at
    today's 00:00 local time, scoped to the kiosk-bound user's library
    via their per-user API key. Returns the same shape as
    get_album_for_slideshow ({id, taken_at, thumbnail_url}) so the
    /api/ambient/slideshow merge step doesn't have to special-case
    either source.

    Empty list on any failure path — kiosk gracefully falls back to
    the album-only set rather than 500'ing the wall. Image type only
    (videos filtered out, same as album path).
    """
    from datetime import datetime, time

    s = _immich_user(user_id)
    if not s:
        return []

    # Local midnight as the cutoff. Immich expects ISO 8601 with TZ;
    # we build it from the local-timezone now() so "today" matches
    # what the user sees on their wall clock, not UTC.
    today_local_midnight = datetime.combine(
        datetime.now().astimezone().date(),
        time.min,
        tzinfo=datetime.now().astimezone().tzinfo,
    ).isoformat()

    body = {
        "takenAfter": today_local_midnight,
        "type":       "IMAGE",
        "size":       max(1, int(limit)),
        "page":       1,
    }
    try:
        r = requests.post(
            f"{s['base_url']}/api/search/metadata",
            json=body,
            headers={**_headers(s), "Content-Type": "application/json"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("get_today_photos: %s", exc)
        return []
    if r.status_code != 200:
        log.warning("get_today_photos: status %s body=%s", r.status_code, r.text[:200])
        return []
    payload = r.json() or {}
    # Response shape: {assets: {items: [...], total, ...}, albums: {...}}
    items = ((payload.get("assets") or {}).get("items")) or []
    items.sort(key=lambda a: (a.get("fileCreatedAt") or ""), reverse=True)
    blocked = exclude_ids or frozenset()
    out: list[dict[str, Any]] = []
    cap = max(1, int(limit))
    for a in items:
        aid = a.get("id")
        if not aid or aid in blocked:
            continue
        out.append({
            "id":            aid,
            "taken_at":      a.get("fileCreatedAt"),
            # Embed the owning Yorik user_id in the URL. The thumbnail
            # proxy honours `?u=` ONLY for kiosk-scope callers (kiosk
            # cookie OR trusted wall-device header), so the household
            # wall can fetch any household member's asset even though
            # its own session cookie maps to one specific user. Regular
            # browser sessions just stay on the caller's library.
            "thumbnail_url": f"/api/photos/{aid}/thumbnail?size=preview&u={user_id}",
        })
        if len(out) >= cap:
            break
    return out


def get_today_photos_workspace(
    user_ids: list[int],
    *,
    limit: int = 200,
    exclude_ids: Optional[frozenset[str]] = None,
) -> list[dict[str, Any]]:
    """Today's photos aggregated across every supplied user. Powers
    the wall's "show today's photos" mode in a shared household
    where every member should see every member's photos roll past.

    Each user_id's Immich library is queried individually — there's
    no shared workspace API key, each user has their own credential
    in credential_store. Per-user failures (no Immich key, network
    blip, 401, etc.) contribute nothing and degrade silently rather
    than 500-ing the whole wall. Results are deduped by asset id,
    sorted by taken_at descending so the merged feed is
    chronological across users (not "all of Dirk then all of
    Anna"), and capped at `limit`.

    To exclude a household member from the wall, drop their PIN /
    disable their account, or — once the per-user "show on kiosk"
    toggle ships — flip that off.
    """
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    blocked = exclude_ids or frozenset()
    for uid in user_ids:
        try:
            per_user = get_today_photos(uid, limit=limit, exclude_ids=blocked)
        except Exception as exc:  # noqa: BLE001 — never 500 the wall
            log.warning("get_today_photos_workspace: uid=%s failed: %s", uid, exc)
            continue
        for p in per_user:
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            merged.append(p)
    merged.sort(key=lambda p: p.get("taken_at") or "", reverse=True)
    return merged[: max(1, int(limit))]


def get_album_for_slideshow(
    album_id: str,
    user_id: str,
    *,
    limit: int = 200,
    exclude_ids: Optional[frozenset[str]] = None,
) -> list[dict[str, Any]]:
    """Fetch up to `limit` photo assets from one album, newest first.
    Returns [{id, taken_at, thumbnail_url}]. thumbnail_url proxies
    through Yorik (/api/photos/{id}/thumbnail?size=preview) so the
    kiosk browser never needs direct Immich auth.

    Empty list when:
      - Immich isn't configured
      - The album doesn't exist or contains 0 assets
      - HTTP error contacting Immich (logged but not raised — kiosk
        falls back to blank wallpaper rather than 500'ing)
    """
    if not album_id:
        return []
    s = _immich_user(user_id)
    if not s:
        return []
    try:
        r = requests.get(f"{s['base_url']}/api/albums/{album_id}",
                         headers=_headers(s), timeout=_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("get_album_for_slideshow %s: %s", album_id, exc)
        return []
    if r.status_code != 200:
        log.warning("get_album_for_slideshow %s: status %s", album_id, r.status_code)
        return []
    payload = r.json() or {}
    assets = payload.get("assets") or []
    # IMAGE only — videos in the album get filtered out (no slideshow
    # play-through; a still wall behaves better than auto-playing video).
    images = [a for a in assets if (a.get("type") or "").upper() == "IMAGE"]
    # Newest first — slideshow then crossfades through them. Optional
    # randomisation could come later as a kiosk-config knob.
    images.sort(key=lambda a: (a.get("fileCreatedAt") or ""), reverse=True)
    blocked = exclude_ids or frozenset()
    out: list[dict[str, Any]] = []
    cap = max(1, int(limit))
    for a in images:
        aid = a.get("id")
        if not aid or aid in blocked:
            continue
        out.append({
            "id":            aid,
            "taken_at":      a.get("fileCreatedAt"),
            # Same proxy URL the /api/skills/find_photo/invoke output
            # uses — see backend/main.py /api/photos/{asset_id}/thumbnail.
            "thumbnail_url": f"/api/photos/{aid}/thumbnail?size=preview",
        })
        if len(out) >= cap:
            break
    return out
