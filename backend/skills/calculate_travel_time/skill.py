"""calculate_travel_time — driving/cycling/walking time between two places.

Defaults `from` to the user's home address from their profile so the
common case ("wie lange nach Hamburg") works with one argument.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


def _user_home_address_variants(user_id: str) -> list[str]:
    """Return progressively-less-specific forms of the user's home address.
    Caller tries each in order until one geocodes — handles obscure street
    names that Nominatim doesn't know without giving up on travel-time
    altogether. Returns [] when the profile has nothing usable."""
    from backend.database import conn_ctx, DEFAULT_DB_PATH
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        row = conn.execute(
            "SELECT address_street, address_postcode, address_city, country "
            "FROM user_profiles WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return []
    street = (row["address_street"] or "").strip()
    plz = (row["address_postcode"] or "").strip()
    city = (row["address_city"] or "").strip()
    country = (row["country"] or "").strip()
    if not (street or plz or city):
        return []
    out: list[str] = []
    plz_city = " ".join(p for p in (plz, city) if p)
    if street and plz_city:
        out.append(", ".join(p for p in (street, plz_city, country) if p))
    if plz_city:
        out.append(", ".join(p for p in (plz_city, country) if p))
    if city and city != plz_city:
        out.append(", ".join(p for p in (city, country) if p))
    # Dedupe while preserving order.
    seen, dedup = set(), []
    for v in out:
        if v not in seen:
            seen.add(v); dedup.append(v)
    return dedup


def _user_home_address(user_id: str) -> Optional[str]:
    """First (most-specific) home address form. Kept for back-compat
    with callers that don't iterate variants."""
    vs = _user_home_address_variants(user_id)
    return vs[0] if vs else None


async def execute(
    ctx,
    to: str,
    **kw: Any,
) -> dict[str, Any]:
    if not (to or "").strip():
        return {
            "_llm_hint": "calculate_travel_time requires `to`.",
            "ok":        False,
        }

    src = (kw.get("from") or "").strip()
    mode = (kw.get("mode") or "driving").lower()

    # Build the list of `from` candidates to try. Explicit user-supplied
    # `from` always wins; otherwise step down from full home address →
    # PLZ+City → City so an obscure street name doesn't kill the call.
    src_candidates: list[str] = (
        [src] if src
        else _user_home_address_variants(getattr(ctx, "user_id", 1))
    )
    if not src_candidates:
        return {
            "_llm_hint": (
                "No `from` address given and the user's profile doesn't "
                "have a home address yet. Either ask the user where they "
                "want to start from, or tell them to fill their address "
                "in Settings → Profile."
            ),
            "ok": False,
        }

    from backend.connectors import invoke as _invoke
    result = None
    last_err = None
    for candidate in src_candidates:
        try:
            result = await _invoke("maps", {
                "op":   "directions",
                "from": candidate,
                "to":   to,
                "mode": mode,
            })
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue
        if result and result.get("ok") is not False:
            break
        last_err = (result or {}).get("error")

    if not result or result.get("ok") is False:
        return {
            "_llm_hint": (
                f"Routing failed: {last_err or 'unknown'}. "
                "Tell the user honestly — don't make up a duration."
            ),
            "ok": False,
        }

    dur_min = int(result.get("duration_min") or 0)
    dur_human = result.get("duration_human") or f"{dur_min} min"
    dist_km = result.get("distance_km") or 0
    provider = result.get("provider") or "osrm"
    mode_word = {"driving": "Auto", "cycling": "Fahrrad", "walking": "zu Fuß"}.get(mode, mode)

    return {
        "_llm_hint": (
            f"shown_to_user: {dur_human} ({dist_km} km) {mode_word} von "
            f"„{result['from'].get('label', src)}\" nach "
            f"„{result['to'].get('label', to)}\". "
            "Quote the duration + distance in the user's language; keep it short."
        ),
        "ok":             True,
        "from":           result.get("from"),
        "to":             result.get("to"),
        "mode":           mode,
        "duration_min":   dur_min,
        "duration_s":     int(result.get("duration_s") or 0),
        "duration_human": dur_human,
        "distance_km":    dist_km,
        "distance_m":     int(result.get("distance_m") or 0),
        "provider":       provider,
    }
