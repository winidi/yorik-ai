"""find_provider_nearby — Overpass-backed POI search via maps connector.

Defaults `near` to the user's home city when omitted so "Apotheke" alone
returns the user's local options instead of failing."""

from __future__ import annotations

import asyncio
from typing import Any, Optional


def _user_home_city(user_id: str) -> Optional[str]:
    from backend.database import conn_ctx, DEFAULT_DB_PATH
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        row = conn.execute(
            "SELECT address_postcode, address_city, country "
            "FROM user_profiles WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    plz = (row["address_postcode"] or "").strip()
    city = (row["address_city"] or "").strip()
    country = (row["country"] or "").strip()
    if not city:
        return None
    parts = [" ".join(p for p in (plz, city) if p)]
    if country:
        parts.append(country)
    return ", ".join(parts)


async def execute(
    ctx,
    poi: str,
    near: Optional[str] = None,
    limit: int = 12,
) -> dict[str, Any]:
    poi = (poi or "").strip()
    if not poi:
        return {"_llm_hint": "find_provider_nearby requires `poi`.",
                "pois": [], "count": 0}

    near = (near or "").strip()
    if not near:
        near = _user_home_city(getattr(ctx, "user_id", 1)) or ""
    if not near:
        return {
            "_llm_hint": (
                f"No `near` location given and the user's profile has no "
                "home city yet. Ask the user where to search (e.g. "
                f"'{poi} in welcher Stadt?')."
            ),
            "pois": [],
            "count": 0,
        }

    try:
        limit_int = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit_int = 12

    from backend.connectors import invoke as _invoke
    try:
        result = await _invoke("maps", {
            "op":    "search_pois",
            "poi":   poi,
            "near":  near,
            "limit": limit_int,
        })
    except Exception as exc:  # noqa: BLE001
        return {
            "_llm_hint": (
                f"Could not search for {poi!r} near {near!r}: {exc}. "
                "Tell the user OpenStreetMap is unreachable; offer to "
                "have them paste an address manually."
            ),
            "pois": [],
            "count": 0,
        }

    if not result or result.get("ok") is False:
        return {
            "_llm_hint": (
                f"POI search failed: {(result or {}).get('error', 'unknown')}. "
                "Tell the user — don't invent businesses."
            ),
            "pois": [],
            "count": 0,
        }

    pois = result.get("pois") or []

    # Emit a ui_action so the chat can render the results as cards
    # (same shape pattern as photos_found / template_picker). The chat
    # app handles 'pois_found' (gracefully degrades to text if not).
    if pois:
        from backend.ui_tools import _append
        _append({
            "type":      "pois_found",
            "poi":       poi,
            "near":      result.get("near", {}).get("label") or near,
            "pois":      pois[:limit_int],
        })

    if not pois:
        hint = (
            f"No {poi} found near {near}. Maybe widen the search area "
            "or try a different keyword. If the user knows the name, "
            "you can add_contact directly and skip the search."
        )
    else:
        names = [p["name"] for p in pois[:5] if p.get("name")]
        hint = (
            f"shown_to_user: {len(pois)} {poi}(s) near {near}: "
            f"{', '.join(names[:3])}{'…' if len(names) > 3 else ''}. "
            "Ask the user which one — when they pick, use add_contact "
            "to save the practice for next time AND add_calendar_event "
            "(with location set) if they want an appointment."
        )

    return {
        "_llm_hint": hint,
        "ok":        True,
        "poi":       poi,
        "near":      result.get("near"),
        "count":     len(pois),
        "pois":      pois,
    }
