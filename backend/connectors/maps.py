"""Maps connector — OpenStreetMap (Nominatim + OSRM + Overpass).

Three operations from one connector:

  - {op: "geocode",      query: "Berliner Str 12, Hamburg"}
                         → {lat, lon, label, kind}
  - {op: "directions",   from: "Hamburg", to: "Berlin", [mode: "driving"]}
                         → {distance_km, duration_min, duration_human, ...}
  - {op: "search_pois",  poi: "dentist", near: "Hannover", [limit: 10]}
                         → {pois: [{name, address, lat, lon, phone?, website?, ...}]}

Default routing backend is the project-osrm.org public demo (free, no key,
rate-limited). When the user pastes an OpenRouteService key into
Settings → Connectors → Maps, we switch to ORS (better uptime, 2k req/day
free tier). Same response shape either way — the skill doesn't care
which provider answered.

Polite usage: Nominatim caps at 1 req/sec per User-Agent and asks for a
descriptive UA — we honour both. Geocode results are cached in-process
for 24h so back-to-back skills (find_provider_nearby → calculate_travel_time)
don't re-resolve the same address twice.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests

from . import ConnectorSpec, register
from .. import credential_store

log = logging.getLogger("homeos.connectors.maps")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1"
ORS_URL = "https://api.openrouteservice.org/v2/directions"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "Yorik/0.3 (https://github.com/winidi/yorik-ai)"
TIMEOUT_S = 8

# In-process geocode cache. {query.lower() → (timestamp, result_dict)}.
# 24h TTL — addresses don't move and skills chain often.
_GEOCODE_CACHE: dict[str, tuple[float, dict]] = {}
_GEOCODE_TTL_S = 24 * 3600

# Overpass POI-type → OSM tag mapping. Generous synonyms so the LLM
# can use natural words; if missing, falls back to a name search.
_POI_TAG_MAP: dict[str, tuple[str, str]] = {
    "dentist":        ("amenity", "dentist"),
    "zahnarzt":       ("amenity", "dentist"),
    "doctor":         ("amenity", "doctors"),
    "arzt":           ("amenity", "doctors"),
    "hausarzt":       ("amenity", "doctors"),
    "pharmacy":       ("amenity", "pharmacy"),
    "apotheke":       ("amenity", "pharmacy"),
    "hospital":       ("amenity", "hospital"),
    "krankenhaus":    ("amenity", "hospital"),
    "veterinary":     ("amenity", "veterinary"),
    "tierarzt":       ("amenity", "veterinary"),
    "school":         ("amenity", "school"),
    "schule":         ("amenity", "school"),
    "kindergarten":   ("amenity", "kindergarten"),
    "bank":           ("amenity", "bank"),
    "atm":            ("amenity", "atm"),
    "post_office":    ("amenity", "post_office"),
    "post":           ("amenity", "post_office"),
    "restaurant":     ("amenity", "restaurant"),
    "cafe":           ("amenity", "cafe"),
    "bar":            ("amenity", "bar"),
    "supermarket":    ("shop", "supermarket"),
    "bakery":         ("shop", "bakery"),
    "bäckerei":       ("shop", "bakery"),
    "hairdresser":    ("shop", "hairdresser"),
    "friseur":        ("shop", "hairdresser"),
    "optician":       ("shop", "optician"),
    "optiker":        ("shop", "optician"),
    "garage":         ("shop", "car_repair"),
    "werkstatt":      ("shop", "car_repair"),
    "petrol_station": ("amenity", "fuel"),
    "tankstelle":     ("amenity", "fuel"),
}


# ───────────────────────── geocoding ─────────────────────────

def _geocode_one(query: str) -> Optional[Dict[str, Any]]:
    key = (query or "").strip().lower()
    if not key:
        return None
    now = time.time()
    cached = _GEOCODE_CACHE.get(key)
    if cached and (now - cached[0]) < _GEOCODE_TTL_S:
        return cached[1]

    r = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    arr = r.json() or []
    if not arr:
        _GEOCODE_CACHE[key] = (now, None)  # type: ignore[assignment]
        return None
    hit = arr[0]
    result = {
        "label": hit.get("display_name"),
        "lat":   float(hit["lat"]),
        "lon":   float(hit["lon"]),
        "kind":  hit.get("type"),
    }
    _GEOCODE_CACHE[key] = (now, result)
    return result


def _resolve(point: Any) -> Optional[Dict[str, Any]]:
    """Accepts a string (geocode it), a {lat, lon} dict (use as-is), or
    a string formatted as 'lat,lon'."""
    if isinstance(point, dict) and "lat" in point and "lon" in point:
        return {"lat": float(point["lat"]), "lon": float(point["lon"]),
                "label": point.get("label", "")}
    if isinstance(point, str):
        s = point.strip()
        # "lat,lon" shortcut
        if "," in s and all(p.strip().replace(".", "").replace("-", "").isdigit()
                             for p in s.split(",", 1)):
            try:
                lat, lon = (float(x.strip()) for x in s.split(",", 1))
                return {"lat": lat, "lon": lon, "label": s}
            except ValueError:
                pass
        return _geocode_one(s)
    return None


# ───────────────────────── routing ─────────────────────────

def _ors_key() -> Optional[str]:
    """Return OpenRouteService API key when configured, else None."""
    creds = credential_store.get("maps") or {}
    key = (creds.get("ors_api_key") or "").strip()
    return key or None


_OSRM_PROFILE_MAP = {
    "driving": "driving", "drive": "driving", "car": "driving",
    "cycling": "cycling", "bike": "cycling", "bicycle": "cycling",
    "walking": "walking", "walk": "walking", "foot": "walking",
}

_ORS_PROFILE_MAP = {
    "driving": "driving-car",
    "cycling": "cycling-regular",
    "walking": "foot-walking",
}


def _route_osrm(a: dict, b: dict, mode: str) -> Optional[dict]:
    profile = _OSRM_PROFILE_MAP.get(mode, "driving")
    coords = f"{a['lon']},{a['lat']};{b['lon']},{b['lat']}"
    r = requests.get(
        f"{OSRM_URL}/{profile}/{coords}",
        params={"overview": "false", "alternatives": "false"},
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    routes = (r.json() or {}).get("routes") or []
    if not routes:
        return None
    rt = routes[0]
    return {
        "distance_m":  int(rt["distance"]),
        "duration_s":  int(rt["duration"]),
        "provider":    "osrm",
    }


def _route_ors(a: dict, b: dict, mode: str, api_key: str) -> Optional[dict]:
    profile = _ORS_PROFILE_MAP.get(mode, "driving-car")
    r = requests.post(
        f"{ORS_URL}/{profile}/geojson",
        headers={
            "Authorization": api_key,
            "Content-Type":  "application/json",
            "User-Agent":    USER_AGENT,
        },
        json={"coordinates": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]]},
        timeout=TIMEOUT_S,
    )
    if r.status_code != 200:
        log.warning("ORS returned %s: %s", r.status_code, r.text[:200])
        return None
    j = r.json()
    feats = (j or {}).get("features") or []
    if not feats:
        return None
    summary = (feats[0].get("properties") or {}).get("summary") or {}
    return {
        "distance_m":  int(summary.get("distance") or 0),
        "duration_s":  int(summary.get("duration") or 0),
        "provider":    "ors",
    }


def _route(a: dict, b: dict, mode: str) -> Optional[dict]:
    """Pick the best available routing backend. ORS when configured
    (paid-tier reliability, no rate-limit headaches), else OSRM (the
    free demo at project-osrm.org — fine for personal use, occasional
    503s)."""
    key = _ors_key()
    if key:
        out = _route_ors(a, b, mode, key)
        if out: return out
        # Fall through to OSRM on failure so the user isn't blocked
        # when ORS quota is exhausted.
    return _route_osrm(a, b, mode)


# ───────────────────────── POI search ─────────────────────────

def _overpass_query(poi: str, near: dict, radius_m: int = 8000,
                    limit: int = 12) -> list[dict]:
    """Overpass QL — radius search around a point. Defaults to 8 km
    which covers a typical small German town + spillover."""
    poi_low = (poi or "").lower().strip()
    tag = _POI_TAG_MAP.get(poi_low)
    if tag:
        key, val = tag
        # Three element types catches POIs tagged as nodes, ways, or
        # relations (Overpass conventions vary by amenity).
        q = (
            f"[out:json][timeout:15];"
            f"("
            f"node[\"{key}\"=\"{val}\"](around:{radius_m},{near['lat']},{near['lon']});"
            f"way[\"{key}\"=\"{val}\"](around:{radius_m},{near['lat']},{near['lon']});"
            f"relation[\"{key}\"=\"{val}\"](around:{radius_m},{near['lat']},{near['lon']});"
            f");"
            f"out tags center {limit};"
        )
    else:
        # Fallback: name substring match. Less precise but catches
        # things like "Zahnarzt Schmidt" when the LLM mis-tagged.
        q = (
            f"[out:json][timeout:15];"
            f"("
            f"node[name~\"{poi}\",i](around:{radius_m},{near['lat']},{near['lon']});"
            f"way[name~\"{poi}\",i](around:{radius_m},{near['lat']},{near['lon']});"
            f");"
            f"out tags center {limit};"
        )
    r = requests.post(
        OVERPASS_URL, data={"data": q},
        headers={"User-Agent": USER_AGENT}, timeout=20,
    )
    r.raise_for_status()
    return ((r.json() or {}).get("elements") or [])[:limit]


def _format_address(tags: dict) -> str:
    parts = []
    street = (tags.get("addr:street") or "").strip()
    hsn = (tags.get("addr:housenumber") or "").strip()
    if street:
        parts.append(f"{street} {hsn}".strip())
    plz = (tags.get("addr:postcode") or "").strip()
    city = (tags.get("addr:city") or "").strip()
    if plz or city:
        parts.append(f"{plz} {city}".strip())
    return ", ".join(parts)


def _poi_from_element(el: dict) -> dict:
    tags = el.get("tags") or {}
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    return {
        "name":     tags.get("name") or tags.get("operator") or "(unbenannt)",
        "address":  _format_address(tags),
        "lat":      lat,
        "lon":      lon,
        "phone":    tags.get("phone") or tags.get("contact:phone"),
        "website":  tags.get("website") or tags.get("contact:website"),
        "email":    tags.get("email")   or tags.get("contact:email"),
        "opening_hours": tags.get("opening_hours"),
        "wheelchair":    tags.get("wheelchair"),
        "osm_id":   f"{el.get('type')}/{el.get('id')}",
    }


# ───────────────────────── op dispatch ─────────────────────────

def maps(op: str, query: str = "", **kw) -> Dict[str, Any]:
    op = (op or "").lower().strip()

    if op == "geocode":
        if not query:
            return {"ok": False, "error": "geocode requires 'query'"}
        result = _geocode_one(query)
        if not result:
            return {"ok": False, "error": f"no match for '{query}'"}
        return {"op": "geocode", **result}

    if op == "directions" or op == "route":
        src = kw.get("from") or ""
        dst = kw.get("to") or ""
        mode = (kw.get("mode") or "driving").lower()
        if not src or not dst:
            return {"ok": False, "error": "directions requires 'from' and 'to'"}
        a = _resolve(src)
        if not a: return {"ok": False, "error": f"could not geocode from={src!r}"}
        # Nominatim politeness ONLY between calls that actually hit Nominatim.
        # When both endpoints came from the cache (or as lat/lon dicts),
        # the sleep is wasted.
        if isinstance(src, str) and not (src.strip().replace(",", "").replace(".", "").replace("-", "").isdigit()):
            time.sleep(1)
        b = _resolve(dst)
        if not b: return {"ok": False, "error": f"could not geocode to={dst!r}"}
        route = _route(a, b, mode)
        if not route:
            return {"ok": False, "error": "no route found"}
        dur_min = round(route["duration_s"] / 60)
        return {
            "op":             "directions",
            "from":           a,
            "to":             b,
            "mode":           mode,
            "distance_m":     route["distance_m"],
            "distance_km":    round(route["distance_m"] / 1000, 1),
            "duration_s":     route["duration_s"],
            "duration_min":   int(dur_min),
            "duration_human": (f"{dur_min // 60}h {dur_min % 60}m"
                                if dur_min >= 60 else f"{dur_min} min"),
            "provider":       route["provider"],
            "source":         f"{route['provider'].upper()} + Nominatim (OpenStreetMap)",
        }

    if op == "search_pois":
        poi = kw.get("poi") or kw.get("query") or ""
        near = kw.get("near") or ""
        try:
            limit = max(1, min(int(kw.get("limit", 12)), 50))
        except (TypeError, ValueError):
            limit = 12
        if not poi:
            return {"ok": False, "error": "search_pois requires 'poi' (e.g. 'dentist', 'apotheke')"}
        if not near:
            return {"ok": False, "error": "search_pois requires 'near' (address or city)"}
        anchor = _resolve(near)
        if not anchor:
            return {"ok": False, "error": f"could not geocode near={near!r}"}
        try:
            elements = _overpass_query(poi, anchor, limit=limit)
        except requests.RequestException as exc:
            return {"ok": False, "error": f"overpass request failed: {exc}"}
        pois = [_poi_from_element(e) for e in elements]
        return {
            "op":      "search_pois",
            "poi":     poi,
            "near":    anchor,
            "count":   len(pois),
            "pois":    pois,
            "source":  "Overpass (OpenStreetMap)",
        }

    if op == "test_connection":
        # Light Nominatim ping so the connector settings page can show
        # a green dot. Doesn't exercise the ORS key — that needs a real
        # route, which costs quota.
        try:
            requests.get(
                NOMINATIM_URL,
                params={"q": "Berlin", "format": "jsonv2", "limit": 1},
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S,
            ).raise_for_status()
            return {"ok": True, "nominatim": "up",
                    "routing_provider": "ors" if _ors_key() else "osrm",
                    "ors_configured": bool(_ors_key())}
        except requests.RequestException as exc:
            return {"ok": False, "error": str(exc)}

    return {"ok": False, "error": f"unknown op '{op}'. Use geocode / directions / search_pois / test_connection."}


register(ConnectorSpec(
    name="maps",
    description=(
        "OpenStreetMap-based maps. Operations: "
        "{op:'geocode', query:'…'} → coords for an address; "
        "{op:'directions', from:'…', to:'…', mode:'driving'|'cycling'|'walking'} "
        "→ travel time + distance via OSRM (default) or OpenRouteService "
        "when an API key is configured; "
        "{op:'search_pois', poi:'dentist', near:'Hannover', limit:12} "
        "→ nearby places of interest via Overpass."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "op":    {"type": "string",
                       "enum": ["geocode", "directions", "route",
                                 "search_pois", "test_connection"]},
            "query": {"type": "string"},
            "from":  {"type": "string"},
            "to":    {"type": "string"},
            "mode":  {"type": "string", "enum": ["driving", "cycling", "walking"]},
            "poi":   {"type": "string"},
            "near":  {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 12},
        },
        "required": ["op"],
    },
    invoke=maps,
    requires_auth=False,  # core ops (Nominatim, OSRM demo, Overpass) need no key
    install_hint=(
        "No setup needed for the free defaults. Optional: paste an "
        "OpenRouteService API key (openrouteservice.org — free 2000 req/day "
        "tier) for higher uptime and better routing accuracy."
    ),
    credentials_schema={
        "type": "object",
        "properties": {
            "ors_api_key": {
                "type":        "string",
                "title":       "OpenRouteService API key (optional)",
                "format":      "password",
                "description": "Paste from https://openrouteservice.org/dev/#/signup — free tier covers ~2000 routing requests/day. Leave empty to use the OSRM public demo.",
            },
        },
    },
    backend="builtin",
    version="1.1",
    tags=["maps", "geocoding", "directions", "routing", "poi", "openstreetmap"],
))
