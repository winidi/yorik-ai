"""Immich connector — voice + LLM access to the user's photo library.

Yorik runs Immich as a separate container (see docker-compose.yml) for the
heavy work: photo storage, EXIF, dedup, ML embedding (CLIP), face
recognition, mobile-app sync. This connector wraps Immich's REST API so the
LLM can answer queries like:

  - "show me photos of the kids from last summer"  →  search
  - "what did we do in Italy in May 2024?"         →  search + date filter
  - "any photos from this week?"                   →  recent
  - "show me Lea's photos"                         →  of_person

The user creates an API key in Immich (Settings → API Keys → New) and
pastes it into the Yorik Settings → Connectors → Immich form. The
base_url is auto-defaulted to localhost:2283 (the docker-compose binding)
but can be overridden if Immich runs elsewhere.

Returned photos are dicts with `id`, `original_name`, `taken_at`, and a
`thumbnail_url` that points back at Yorik (`/api/photos/{id}/thumbnail`).
The browser never sees the Immich API key — Yorik proxies the fetch with
the calling user's per-user key server-side. Do NOT change this to sign
the URL with the key directly; that would leak per-user Immich creds to
the browser and (in multi-tenant deployments) to any LLM call that sees
its own message log.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

from . import ConnectorSpec, register
from .. import credential_store

log = logging.getLogger("homeos.connectors.immich")

DEFAULT_BASE_URL = "http://localhost:2283"
TIMEOUT_S = 8
MAX_RETURN = 30


# Reachable probe — cached for 30s so the dispatch gate, /api/health,
# and the Photos iframe overlay can all share one cheap check without
# hammering the Immich container.
_REACH_CACHE_TTL_S = 30.0
_reach_cache: Dict[str, Any] = {"checked_at": 0.0, "ok": False, "reason": ""}


def _immich_probe(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Quick ping of Immich's /api/server/ping. Returns {ok, reason}.
    Cached for 30s to keep the cost negligible when called from multiple
    places per page-load."""
    now = time.time()
    if (now - _reach_cache["checked_at"]) < _REACH_CACHE_TTL_S:
        return {"ok": _reach_cache["ok"], "reason": _reach_cache["reason"]}
    c = override or _creds()
    base = (c.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    ok = False
    reason = ""
    try:
        r = requests.get(f"{base}/api/server/ping", timeout=1.5)
        if r.ok:
            ok = True
        else:
            reason = f"HTTP {r.status_code} from /api/server/ping"
    except requests.exceptions.ConnectTimeout:
        reason = "connect timeout"
    except requests.exceptions.ConnectionError:
        reason = "connection refused"
    except requests.RequestException as exc:
        reason = f"{type(exc).__name__}: {exc}"
    _reach_cache["checked_at"] = now
    _reach_cache["ok"] = ok
    _reach_cache["reason"] = reason
    return {"ok": ok, "reason": reason, "base_url": base}


def immich_reachable(override: Optional[Dict[str, Any]] = None) -> bool:
    return _immich_probe(override)["ok"]


def _creds() -> Dict[str, Any]:
    c = credential_store.get("immich") or {}
    c.setdefault("base_url", DEFAULT_BASE_URL)
    return c


def _headers(c: Dict[str, Any]) -> Dict[str, str]:
    return {
        "x-api-key": c.get("api_key", ""),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _photo_dict(asset: Dict[str, Any], base_url: str, api_key: str) -> Dict[str, Any]:
    """Project an Immich asset into the slim shape the LLM/frontend uses."""
    aid = asset.get("id")
    return {
        "id": aid,
        "original_name": asset.get("originalFileName") or "",
        "taken_at": asset.get("fileCreatedAt") or asset.get("localDateTime"),
        "type": asset.get("type"),  # "IMAGE" | "VIDEO"
        # Yorik-relative — proxied by backend/main.py
        # /api/photos/{id}/thumbnail so the URL works from any device
        # that can reach Yorik (not just localhost on the host where
        # the laptop runs).
        "thumbnail_url": f"/api/photos/{aid}/thumbnail?size=preview",
        # view_url stays absolute because clicking it opens the full
        # Immich UI, which is served at its own host:port. The Photos
        # React app's deep-link `/r/photos?asset=<id>` is a separate
        # path for the in-app preview.
        "view_url": f"{base_url}/photos/{aid}",
    }


def _resolve_person_ids(names: List[str],
                         override: Optional[Dict[str, Any]] = None) -> List[str]:
    """Resolve a list of person names to Immich personIds via /api/people.
    Case-insensitive exact match. Unknown names are silently dropped —
    the caller decides whether to fall back to a person-less search.
    Returns [] on Immich failure or empty input."""
    if not names:
        return []
    c = override or _creds()
    if not c.get("api_key"):
        return []
    base = c["base_url"].rstrip("/")
    try:
        rp = requests.get(f"{base}/api/people", headers=_headers(c),
                          params={"withHidden": "false"}, timeout=TIMEOUT_S)
        rp.raise_for_status()
        all_people = (rp.json() or {}).get("people", []) or []
        by_name = {(p.get("name") or "").lower(): p.get("id")
                   for p in all_people if p.get("name") and p.get("id")}
        resolved: List[str] = []
        for n in names:
            n_clean = (n or "").strip()
            if not n_clean:
                continue
            pid = by_name.get(n_clean.lower())
            if pid:
                resolved.append(pid)
        return resolved
    except Exception as exc:  # noqa: BLE001
        log.info("person resolution failed for %r: %s", names, exc)
        return []


def _search(query: str, take_count: int,
            override: Optional[Dict[str, Any]] = None,
            person_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    c = override or _creds()
    if not c.get("api_key"):
        return []
    base = c["base_url"].rstrip("/")
    # Smart search uses CLIP embeddings under the hood.
    body: Dict[str, Any] = {"query": query, "size": min(take_count, MAX_RETURN)}
    if person_ids:
        # SmartSearchDto extends BaseSearchDto which exposes personIds —
        # Immich AND's them with the CLIP query, so "fotos von dirk in
        # einem anzug" lands as one call: query='anzug', personIds=[dirk]
        # rather than CLIP-only (random) or face-only (ignores 'anzug').
        body["personIds"] = list(person_ids)
    r = requests.post(
        f"{base}/api/search/smart",
        headers=_headers(c),
        json=body,
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    items = (r.json() or {}).get("assets", {}).get("items", []) or []
    return [_photo_dict(a, base, c["api_key"]) for a in items[:take_count]]


def _recent(days: int, take_count: int, override: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Most recent assets, regardless of content. days is informational
    only — Immich's /api/search/metadata orders newest first; we paginate
    one page of `take_count` and let the caller filter if needed."""
    c = override or _creds()
    if not c.get("api_key"):
        return []
    base = c["base_url"].rstrip("/")
    r = requests.post(
        f"{base}/api/search/metadata",
        headers=_headers(c),
        json={"order": "desc", "size": min(take_count, MAX_RETURN), "type": "IMAGE"},
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    items = (r.json() or {}).get("assets", {}).get("items", []) or []
    return [_photo_dict(a, base, c["api_key"]) for a in items[:take_count]]


def _taken_on(start_iso: str, end_iso: str, take_count: int = 12,
              exclude_whatsapp: bool = True,
              override: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Photos *taken* in a date range, by EXIF/file metadata date.

    Distinct from _recent_uploads — that's server-upload-date ("when did
    this land in Immich"). This one is the photo's actual capture date
    ("when did I press the shutter"), which is what users mean by "show
    me today's photos". Immich exposes both as separate filters; we use
    `takenAfter` / `takenBefore` for this one.

    Same `exclude_whatsapp` filename filter as _recent_uploads so chat
    auto-imports don't pollute the briefing.
    """
    c = override or _creds()
    if not c.get("api_key"):
        return []
    base = c["base_url"].rstrip("/")
    try:
        r = requests.post(
            f"{base}/api/search/metadata",
            headers=_headers(c),
            json={
                "takenAfter":  start_iso,
                "takenBefore": end_iso,
                "order": "desc",
                "size": min(take_count * 3, MAX_RETURN),
                "type": "IMAGE",
            },
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        items = (r.json() or {}).get("assets", {}).get("items", []) or []
    except Exception as exc:  # noqa: BLE001
        log.info("taken_on query failed: %s", exc)
        return []

    if exclude_whatsapp:
        items = [a for a in items
                 if not (a.get("originalFileName") or "").lower().startswith("whatsapp-")]
    return [_photo_dict(a, base, c["api_key"]) for a in items[:take_count]]


def _recent_uploads(hours: int = 24, take_count: int = 6,
                    exclude_whatsapp: bool = True,
                    override: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Assets the user uploaded into Immich within the last `hours`.

    Distinct from _recent — that orders by `fileCreatedAt` (EXIF date,
    so a 2019 vacation photo imported today would NOT appear). This one
    asks Immich for assets created on the server within the window,
    which matches "I just uploaded these" — the natural meaning for a
    morning briefing card.

    `exclude_whatsapp=True` (default) strips files whose original name
    starts with `whatsapp-` — that's the prefix our WhatsApp ingestion
    uses, so this filter independently shields the briefing from chat
    photo noise even before the toggle in whatsapp_media takes effect
    for older installs.

    Best-effort: silent empty-return on any Immich-side error so a
    failing Immich never blocks the dashboard digest endpoint.
    """
    c = override or _creds()
    if not c.get("api_key"):
        return []
    base = c["base_url"].rstrip("/")
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        r = requests.post(
            f"{base}/api/search/metadata",
            headers=_headers(c),
            json={
                "createdAfter": since,  # server-side upload time, not EXIF
                "order": "desc",
                "size": min(take_count * 3, MAX_RETURN),  # over-fetch to absorb the WA filter
                "type": "IMAGE",
            },
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        items = (r.json() or {}).get("assets", {}).get("items", []) or []
    except Exception as exc:  # noqa: BLE001
        log.info("recent uploads query failed: %s", exc)
        return []

    if exclude_whatsapp:
        items = [a for a in items
                 if not (a.get("originalFileName") or "").lower().startswith("whatsapp-")]
    return [_photo_dict(a, base, c["api_key"]) for a in items[:take_count]]


def _of_person(name: str, take_count: int, override: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Photos containing a named face. Person must already be named in
    Immich (the user labels face clusters in the People page)."""
    c = override or _creds()
    if not c.get("api_key"):
        return []
    base = c["base_url"].rstrip("/")
    # Look up the person id by name first.
    rp = requests.get(
        f"{base}/api/people",
        headers=_headers(c),
        params={"withHidden": "false"},
        timeout=TIMEOUT_S,
    )
    rp.raise_for_status()
    people = (rp.json() or {}).get("people", []) or []
    match = next((p for p in people if (p.get("name") or "").lower() == name.lower()), None)
    if not match:
        return []
    person_id = match["id"]
    r = requests.post(
        f"{base}/api/search/metadata",
        headers=_headers(c),
        json={"personIds": [person_id], "size": min(take_count, MAX_RETURN), "order": "desc"},
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    items = (r.json() or {}).get("assets", {}).get("items", []) or []
    return [_photo_dict(a, base, c["api_key"]) for a in items[:take_count]]


# ── German ↔ English place-name aliases ──────────────────────────────
#
# Immich's reverse geocoder (Nominatim by default) stores country and
# major-city names in English. German-speaking users naturally type
# "Türkei" / "Italien" / "München" — those find zero matches against
# an Immich library storing "Turkey" / "Italy" / "Munich". This
# bidirectional map lets either form work. Bidirectional means typing
# the English form against a German-locale Immich also works.

_LOCATION_ALIASES: Dict[str, List[str]] = {
    # countries — post-rename forms (Türkiye 2022, North Macedonia 2019,
    # Eswatini 2018, Cabo Verde 2013, Myanmar 1989) all get mapped both
    # ways so modern Nominatim data AND older photo libraries both match.
    "türkei":   ["Türkiye", "Turkey"],
    "türkiye":  ["Türkei", "Turkey"],
    "turkey":   ["Türkiye", "Türkei"],
    "italien": ["Italy"],          "italy": ["Italien"],
    "spanien": ["Spain"],          "spain": ["Spanien"],
    "frankreich": ["France"],      "france": ["Frankreich"],
    "griechenland": ["Greece"],    "greece": ["Griechenland"],
    "polen": ["Poland"],           "poland": ["Polen"],
    "tschechien": ["Czechia", "Czech Republic"],
    "czechia": ["Tschechien"],     "czech republic": ["Tschechien"],
    "niederlande": ["Netherlands"], "netherlands": ["Niederlande"],
    "österreich": ["Austria"],     "austria": ["Österreich"],
    "oesterreich": ["Austria", "Österreich"],
    "schweiz": ["Switzerland"],    "switzerland": ["Schweiz"],
    "dänemark": ["Denmark"],       "denmark": ["Dänemark"],
    "daenemark": ["Denmark", "Dänemark"],
    "schweden": ["Sweden"],        "sweden": ["Schweden"],
    "norwegen": ["Norway"],        "norway": ["Norwegen"],
    "finnland": ["Finland"],       "finland": ["Finnland"],
    "großbritannien": ["United Kingdom", "Great Britain"],
    "grossbritannien": ["United Kingdom", "Great Britain"],
    "vereinigtes königreich": ["United Kingdom"],
    "uk": ["United Kingdom"],
    "usa": ["United States", "United States of America"],
    "vereinigte staaten": ["United States"],
    "kroatien": ["Croatia"],       "croatia": ["Kroatien"],
    "ungarn": ["Hungary"],         "hungary": ["Ungarn"],
    "belgien": ["Belgium"],        "belgium": ["Belgien"],
    "irland": ["Ireland"],         "ireland": ["Irland"],
    "ägypten": ["Egypt"],          "egypt": ["Ägypten"],
    "marokko": ["Morocco"],        "morocco": ["Marokko"],
    "thailand": ["Thailand"],
    "japan": ["Japan"],
    "deutschland": ["Germany"],    "germany": ["Deutschland"],
    # North Macedonia (formerly Macedonia, renamed 2019).
    "nordmazedonien":  ["North Macedonia"],
    "mazedonien":      ["North Macedonia"],
    "north macedonia": ["Nordmazedonien"],
    "macedonia":       ["North Macedonia", "Nordmazedonien"],
    # Cabo Verde (formerly Cape Verde, renamed 2013).
    "kap verde":  ["Cabo Verde", "Cape Verde"],
    "kapverden":  ["Cabo Verde", "Cape Verde"],
    "cabo verde": ["Cape Verde"],
    "cape verde": ["Cabo Verde"],
    # Eswatini (formerly Swaziland / DE: Swasiland, renamed 2018).
    "eswatini":  ["Eswatini"],
    "swasiland": ["Eswatini"],
    "swaziland": ["Eswatini"],
    # Myanmar (formerly Burma / DE: Birma, renamed 1989 but old names
    # linger in informal speech).
    "birma": ["Myanmar"],
    "burma": ["Myanmar"],
    # major German cities exonymised in English
    "münchen": ["Munich"],         "munich": ["München"],
    "muenchen": ["Munich", "München"],
    "köln": ["Cologne"],           "cologne": ["Köln"],
    "koeln": ["Cologne", "Köln"],
    "nürnberg": ["Nuremberg"],     "nuremberg": ["Nürnberg"],
    "nuernberg": ["Nuremberg", "Nürnberg"],
    "wien": ["Vienna"],            "vienna": ["Wien"],
    "venedig": ["Venice"],         "venice": ["Venedig"],
    "rom": ["Rome"],               "rome": ["Rom"],
    "florenz": ["Florence"],       "florence": ["Florenz"],
    "mailand": ["Milan"],          "milan": ["Mailand"],
    "neapel": ["Naples"],          "naples": ["Neapel"],
    "athen": ["Athens"],           "athens": ["Athen"],
    "prag": ["Prague"],            "prague": ["Prag"],
    "warschau": ["Warsaw"],        "warsaw": ["Warschau"],
    "moskau": ["Moscow"],          "moscow": ["Moskau"],
    "lissabon": ["Lisbon"],        "lisbon": ["Lissabon"],
    "kopenhagen": ["Copenhagen"],  "copenhagen": ["Kopenhagen"],
    "stockholm": ["Stockholm"],
    "istanbul": ["Istanbul", "İstanbul"],
}


def _location_variants(loc: str) -> List[str]:
    """Return all the forms we should try against Immich for a given
    user-typed place name: the original, title-cased, and any known
    German↔English aliases. Order matters — first non-empty match wins
    in the calling _filter() loop, and exact-user-input goes first so
    a typo on a real Immich-stored name doesn't get masked by a
    correctly-spelled alias."""
    loc = (loc or "").strip()
    if not loc:
        return []
    variants: List[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = (s or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            variants.append(s)

    _add(loc)
    # Title-case fallback — Immich stores values title-cased
    _add(loc.title())
    # Locale aliases — looked up case-insensitive
    for alias in _LOCATION_ALIASES.get(loc.lower(), []):
        _add(alias)
        _add(alias.title())
    return variants


def _unnamed_people(take_count: int = 8,
                    override: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Back-compat wrapper. Old callers asked for unnamed-only; we
    now expose the mixed list via _people_for_picker but keep this
    name as a thin filter so anything still importing the old symbol
    works."""
    return [p for p in _people_for_picker(take_count, override) if not p.get("name")]


def _people_for_picker(take_count: int = 36,
                       override: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Top-N face clusters from Immich, sorted by face count
    (most-photographed first — likely a family member / close
    contact). INCLUDES already-named clusters so the chat picker
    can offer them when the user's search term doesn't match an
    existing label (e.g. typed "Sara", labeled "Sarah").

    Each carries a pre-authed thumbnail URL the chat can render
    directly + an optional `name` field. When name is present,
    tapping the tile re-runs the original skill with that name as
    the person arg (no rename). When name is empty, the picker's
    existing assign-and-label flow runs."""
    c = override or _creds()
    if not c.get("api_key"):
        return []
    base = c["base_url"].rstrip("/")
    try:
        r = requests.get(
            f"{base}/api/people",
            headers=_headers(c),
            # Pull everyone; we sort + cap client-side. Page-size 500
            # covers a household-scale corpus easily.
            params={"withHidden": "false", "size": 500},
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        all_people = (r.json() or {}).get("people", []) or []
    except Exception as exc:  # noqa: BLE001
        log.info("people_for_picker lookup failed: %s", exc)
        return []
    # Immich exposes face count as either `faceCount` or
    # `numberOfPhotos` depending on version — try both.
    all_people.sort(key=lambda p: -int(
        p.get("faceCount") or p.get("numberOfPhotos") or 0
    ))
    return [{
        "id":            p["id"],
        # Yorik-relative — proxied by /api/photos/people/{id}/thumbnail
        # so the URL works from any device that can reach Yorik (not
        # just localhost on the host).
        "thumbnail_url": f"/api/photos/people/{p['id']}/thumbnail",
        "face_count":    int(p.get("faceCount") or p.get("numberOfPhotos") or 0),
        "name":          (p.get("name") or "").strip() or None,
    } for p in all_people[:take_count]]


def _label_person(person_id: str, name: str,
                  override: Optional[Dict[str, Any]] = None) -> bool:
    """Set the name on an Immich face cluster. Returns True on success.
    Busts the in-process _known_people_cache so the next find_photo
    call sees the new label without waiting 60s."""
    c = override or _creds()
    if not c.get("api_key"):
        return False
    base = c["base_url"].rstrip("/")
    try:
        r = requests.put(
            f"{base}/api/people/{person_id}",
            headers=_headers(c),
            json={"name": name},
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("label_person %s='%s' failed: %s", person_id, name, exc)
        return False
    # Cache bust — see find_photo/skill.py _known_people_cache for shape.
    try:
        from backend.skills.find_photo import skill as _fp_skill
        if hasattr(_fp_skill, "_known_people_cache"):
            _fp_skill._known_people_cache["at"] = 0
    except Exception:  # noqa: BLE001
        pass
    return True


def _lookup_album_id(name: str, override: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Resolve an album name to its Immich UUID. Case-insensitive
    substring match, longest-name-first so "Hochzeit 2024" beats
    "Hochzeit" when both exist. Returns None if no albums match."""
    c = override or _creds()
    if not c.get("api_key"):
        return None
    base = c["base_url"].rstrip("/")
    try:
        r = requests.get(f"{base}/api/albums", headers=_headers(c), timeout=TIMEOUT_S)
        r.raise_for_status()
        albums = r.json() or []
    except Exception as exc:  # noqa: BLE001
        log.info("album lookup failed: %s", exc)
        return None
    needle = (name or "").strip().lower()
    if not needle:
        return None
    # Exact match wins; otherwise longest substring match.
    exact = next((a for a in albums if (a.get("albumName") or "").lower() == needle), None)
    if exact:
        return exact.get("id")
    matches = [a for a in albums if needle in (a.get("albumName") or "").lower()]
    if not matches:
        return None
    matches.sort(key=lambda a: -len(a.get("albumName") or ""))
    return matches[0].get("id")


def _filter(
    *,
    take_count: int = 12,
    start_iso: Optional[str] = None,
    end_iso: Optional[str] = None,
    location: Optional[str] = None,
    favorites_only: bool = False,
    album: Optional[str] = None,
    # Multi-person: pass a list of names. Each is resolved to a People
    # UUID; all are AND'd in the Immich query (`personIds=[a, b, c]`
    # means "photos containing person a AND b AND c"). Single-name
    # callers use the back-compat `person` arg below.
    people: Optional[List[str]] = None,
    person: Optional[str] = None,
    media_type: Optional[str] = None,  # 'IMAGE' | 'VIDEO' | None=both
    camera_make: Optional[str] = None,
    camera_model: Optional[str] = None,
    exclude_archived: bool = True,
    exclude_whatsapp: bool = True,
    override: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Unified metadata search. Combines ANY subset of: date range +
    location (city / state / country) + favorite + album + person +
    media type + camera. Immich's /api/search/metadata accepts them
    all in one call so combining is free (no fan-out, one request).

    Location matching: Immich's metadata endpoint takes `city`,
    `state`, `country` as separate exact-match fields. We try all
    three in priority order (city → state → country) — first hit
    wins. Substring matching isn't supported by the API; if you need
    fuzzy place-name matching, fall back to op='search' with CLIP."""
    c = override or _creds()
    if not c.get("api_key"):
        return []
    base = c["base_url"].rstrip("/")

    body: Dict[str, Any] = {
        "size":  min(max(take_count * 3, 12), MAX_RETURN),
        "order": "desc",
    }
    if start_iso:        body["takenAfter"]  = start_iso
    if end_iso:          body["takenBefore"] = end_iso
    if favorites_only:   body["isFavorite"]  = True
    if exclude_archived: body["isArchived"]  = False
    if media_type:       body["type"]        = media_type.upper()
    if camera_make:      body["make"]        = camera_make
    if camera_model:     body["model"]       = camera_model

    # Build the combined people list. Single-name `person` arg folds in
    # as one element of `people` for the multi-AND lookup; dupes removed.
    name_list: List[str] = []
    if people:
        name_list.extend([n.strip() for n in people if n and n.strip()])
    if person and person.strip() and person.strip() not in name_list:
        name_list.append(person.strip())
    if name_list:
        resolved_ids = _resolve_person_ids(name_list, override=c)
        if resolved_ids:
            # Immich AND's the personIds — "people=[A, B]" returns photos
            # containing BOTH A and B, not either-or. Exactly the
            # "Bilder von mir UND Sara" semantics the user wants.
            body["personIds"] = resolved_ids

    if album:
        aid = _lookup_album_id(album, override=c)
        if aid:
            body["albumIds"] = [aid]

    # Location: try city, then state, then country. We don't combine
    # them — Immich treats these as AND'd exact matches and most
    # casual queries land on one of the three.
    #
    # Locale fanout: Immich's reverse geocoder stores country/city
    # names in English (Nominatim's default), so a user typing
    # "Türkei" / "Italien" / "München" against the German UI would
    # hit zero results against the English-stored "Turkey" / "Italy" /
    # "Munich". We expand each input to its known variants so either
    # form works. Title-cased variant is also added because Immich
    # stores values title-cased ("Stuttgart" not "stuttgart"), and
    # users often type queries lowercase in chat.
    if location:
        loc = location.strip()
        variants = _location_variants(loc)
        location_attempts: List[Dict[str, str]] = []
        for v in variants:
            location_attempts.append({"city":    v})
            location_attempts.append({"state":   v})
            location_attempts.append({"country": v})
    else:
        location_attempts = [{}]  # single pass, no location filter

    items: List[Dict[str, Any]] = []
    for loc_filter in location_attempts:
        attempt_body = {**body, **loc_filter}
        try:
            r = requests.post(
                f"{base}/api/search/metadata",
                headers=_headers(c),
                json=attempt_body,
                timeout=TIMEOUT_S,
            )
            r.raise_for_status()
            items = (r.json() or {}).get("assets", {}).get("items", []) or []
        except Exception as exc:  # noqa: BLE001
            log.info("filter query (%s) failed: %s",
                     ",".join(loc_filter.keys()) or "no-loc", exc)
            continue
        if items:
            break  # first non-empty match wins

    if exclude_whatsapp:
        items = [a for a in items
                 if not (a.get("originalFileName") or "").lower().startswith("whatsapp-")]
    return [_photo_dict(a, base, c["api_key"]) for a in items[:take_count]]


def _stats(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Library-wide counts from Immich's server statistics endpoint.

    Returns {total_photos, total_videos, total_assets, usage_bytes} when
    reachable, or {} on any failure (don't break the search path just
    because the stats endpoint isn't available — older Immich builds had
    a different URL).
    """
    c = override or _creds()
    if not c.get("api_key"):
        return {}
    base = c["base_url"].rstrip("/")
    # Try the modern path first; fall back to the legacy one. Both are
    # admin-scoped; per-user keys may 403 — caller treats {} as "unknown".
    for path in ("/api/server/statistics", "/api/server-info/statistics", "/api/server-info/stats"):
        try:
            r = requests.get(f"{base}{path}", headers=_headers(c), timeout=TIMEOUT_S)
            if not r.ok:
                continue
            j = r.json() or {}
            return {
                "total_photos": int(j.get("photos") or j.get("imageCount") or 0),
                "total_videos": int(j.get("videos") or j.get("videoCount") or 0),
                "total_assets": int((j.get("photos") or 0) + (j.get("videos") or 0)
                                    or j.get("totalAssetCount") or 0),
                "usage_bytes":  int(j.get("usage") or j.get("usageCount") or 0),
            }
        except (requests.RequestException, ValueError):
            continue
    return {}


def immich(op: str, query: str = "", days: int = 7, person: str = "",
           take_count: int = 12,
           start_iso: str = "", end_iso: str = "",
           # New filter args — combine freely. Only used by op='filter'.
           location: str = "", favorites_only: bool = False,
           album: str = "", media_type: str = "",
           camera_make: str = "", camera_model: str = "",
           people: Optional[List[str]] = None,
           creds_override: Optional[Dict[str, Any]] = None,
           **_kw) -> Dict[str, Any]:
    """Top-level dispatch. `creds_override` (wave-3) lets a caller pass
    per-user {base_url, api_key} so the request runs with that user's
    permissions in Immich — Anna's search uses Anna's key. Falls back
    to the global admin key from credential_store when None."""
    op = (op or "").lower().strip()
    creds_arg = creds_override  # close over for the helpers below
    # Reachable gate — every op needs to talk to Immich, so do it once at
    # the top instead of letting each helper silently return [] on
    # ConnectionError (which made an outage look identical to "no photos
    # found" and prompted the LLM to confidently say so). The probe is
    # 30s-cached so this costs nothing on the happy path.
    probe = _immich_probe(creds_arg)
    if not probe["ok"]:
        return {
            "ok":        False,
            "op":        op,
            "photos":    [],
            "error":     f"Immich unreachable ({probe['reason']}). Is the yorik-immich-server container running? Try: docker compose up -d immich-server",
            "degraded":  True,
        }
    try:
        # Empty-search intent: "show me a photo" / "wie viele fotos habe ich"
        # → treat as recent + always include stats so the LLM has the count.
        if op == "search" and not (query or "").strip():
            op = "recent"
        if op == "stats":
            return {"op": "stats", **_stats(creds_arg)}
        if op == "search":
            # Resolve any person / people args to personIds so CLIP and
            # face filters AND together in one call. Falls back to a
            # pure CLIP search when no names match (unknown name) —
            # better than 403'ing a typo'd query.
            name_list: List[str] = []
            if people:
                name_list.extend([n for n in people if n])
            if person and person.strip() and person.strip() not in name_list:
                name_list.append(person.strip())
            pids = _resolve_person_ids(name_list, creds_arg) if name_list else None
            return {"op": "search", "query": query,
                    "person": person or None, "people": people or None,
                    "photos": _search(query, take_count, creds_arg, person_ids=pids),
                    **_stats(creds_arg)}
        if op == "recent":
            return {"op": "recent", "days": days,
                    "photos": _recent(days, take_count, creds_arg),
                    **_stats(creds_arg)}
        if op == "of_person":
            if not person:
                return {"ok": False, "error": "of_person requires 'person'"}
            photos = _of_person(person, take_count, creds_arg)
            return {"op": "of_person", "person": person, "photos": photos,
                    "warning": "no person matched — name your face clusters in Immich → People first" if not photos else None}
        if op == "taken_on":
            if not (start_iso and end_iso):
                return {"ok": False, "error": "taken_on requires start_iso + end_iso"}
            return {"op": "taken_on", "start_iso": start_iso, "end_iso": end_iso,
                    "photos": _taken_on(start_iso, end_iso, take_count, True, creds_arg)}
        if op == "filter":
            # Combined metadata search — any subset of {location, date,
            # favorite, album, person, media_type, camera} freely
            # composable in a single Immich query. Returns photos +
            # echoes back the filters that were applied so the LLM
            # narrates accurately ("3 favorites from Berlin in 2023").
            photos = _filter(
                take_count=take_count,
                start_iso=start_iso or None, end_iso=end_iso or None,
                location=location or None,
                favorites_only=favorites_only,
                album=album or None,
                people=people or None,
                person=person or None,
                media_type=media_type or None,
                camera_make=camera_make or None,
                camera_model=camera_model or None,
                override=creds_arg,
            )
            return {
                "op": "filter",
                "filters_applied": {
                    "location":       location or None,
                    "favorites_only": bool(favorites_only),
                    "album":          album or None,
                    "people":         people or ([person] if person else None),
                    "media_type":     media_type or None,
                    "camera_make":    camera_make or None,
                    "camera_model":   camera_model or None,
                    "start_iso":      start_iso or None,
                    "end_iso":        end_iso or None,
                },
                "photos": photos,
            }
        if op == "test_connection":
            c = creds_arg or _creds()
            base = c["base_url"].rstrip("/")
            r = requests.get(f"{base}/api/server/ping", timeout=TIMEOUT_S)
            return {"ok": r.ok, "status": r.status_code, "base_url": base,
                    "has_api_key": bool(c.get("api_key"))}
        return {"ok": False, "error": f"unknown op '{op}'. Use search/recent/of_person/test_connection."}
    except requests.RequestException as exc:
        return {"ok": False, "error": f"immich request failed: {exc}"}


register(ConnectorSpec(
    name="immich",
    description=(
        "Search the user's photo library (powered by Immich, running locally). "
        "Operations: "
        "{op: 'search', query: 'beach sunset'} for content search via CLIP; "
        "{op: 'recent', days: 7} for newest photos; "
        "{op: 'of_person', person: 'Lea'} for face-filtered (person must be named in Immich first)."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["search", "recent", "of_person", "test_connection"]},
            "query": {"type": "string", "description": "Free-text content query (for op=search)"},
            "days": {"type": "integer", "default": 7, "description": "Look-back window in days (for op=recent)"},
            "person": {"type": "string", "description": "Named person (for op=of_person)"},
            "take_count": {"type": "integer", "default": 12, "minimum": 1, "maximum": 30},
        },
        "required": ["op"],
    },
    invoke=immich,
    requires_auth=True,
    install_hint=(
        "Open Photos in Yorik (or visit https://<your-tailnet-host>:8443), set the admin "
        "password if not yet done, then Settings → API Keys → New API Key. Copy the key "
        "and paste it here."
    ),
    credentials_schema={
        "type": "object",
        "required": ["api_key"],
        "properties": {
            "base_url": {
                "type": "string",
                "title": "Immich URL",
                "default": "http://localhost:2283",
                "description": "Where Immich is reachable from this Yorik box (not from your tablet — that's localhost:2283 in 99% of installs).",
            },
            "api_key": {
                "type": "string",
                "title": "API Key",
                "format": "password",
                "description": "Create one in Immich → Account → API Keys → New.",
            },
        },
    },
    backend="builtin",
    version="1.0",
    tags=["photos", "immich", "vision", "local"],
))
