"""Weather connector — uses open-meteo.com (free, no API key, generous limits).

Two calls under the hood:
  1. Geocoding API to turn "Berlin" → lat/lon
  2. Forecast API to get current weather at those coords

Returns a normalized shape so layouts that depend on `weather/v1` always get
the same fields regardless of which provider we swap in later.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from . import ConnectorSpec, register

log = logging.getLogger("homeos.connectors.weather")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 6

# WMO weather codes → (short label, emoji icon). open-meteo returns the code.
_WMO: Dict[int, tuple[str, str]] = {
    0: ("clear", "☀️"),
    1: ("mostly clear", "🌤"),
    2: ("partly cloudy", "⛅"),
    3: ("overcast", "☁️"),
    45: ("foggy", "🌫"),
    48: ("rime fog", "🌫"),
    51: ("light drizzle", "🌦"),
    53: ("drizzle", "🌦"),
    55: ("dense drizzle", "🌧"),
    61: ("light rain", "🌧"),
    63: ("rain", "🌧"),
    65: ("heavy rain", "⛈"),
    71: ("light snow", "🌨"),
    73: ("snow", "🌨"),
    75: ("heavy snow", "❄️"),
    77: ("snow grains", "❄️"),
    80: ("light showers", "🌦"),
    81: ("showers", "🌧"),
    82: ("violent showers", "⛈"),
    85: ("light snow showers", "🌨"),
    86: ("snow showers", "❄️"),
    95: ("thunderstorm", "⛈"),
    96: ("thunderstorm + hail", "⛈"),
    99: ("severe thunderstorm", "⛈"),
}


def _geocode(city: str) -> Optional[Dict[str, Any]]:
    r = requests.get(GEOCODE_URL, params={"name": city, "count": 1, "language": "en"}, timeout=TIMEOUT_S)
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        return None
    return results[0]


def weather(city: str, units: str = "celsius") -> Dict[str, Any]:
    """Current weather in `city`. units = celsius | fahrenheit."""
    loc = _geocode(city)
    if not loc:
        return {"ok": False, "error": f"could not geocode '{city}'"}
    params = {
        "latitude": loc["latitude"],
        "longitude": loc["longitude"],
        "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
        "timezone": "auto",
        "temperature_unit": "fahrenheit" if units.startswith("f") else "celsius",
    }
    r = requests.get(FORECAST_URL, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    j = r.json()
    cur = j.get("current") or {}
    code = int(cur.get("weather_code") or 0)
    label, icon = _WMO.get(code, (f"code {code}", ""))
    return {
        "city": loc["name"],
        "country": loc.get("country"),
        "temp": round(float(cur.get("temperature_2m") or 0), 1),
        "temp_unit": "°F" if units.startswith("f") else "°C",
        "condition": label,
        "icon": icon,
        "humidity_pct": cur.get("relative_humidity_2m"),
        "wind_kmh": cur.get("wind_speed_10m"),
        "as_of": cur.get("time"),
        "source": "open-meteo",
    }


register(ConnectorSpec(
    name="weather",
    description=(
        "Get current weather for a city. Returns temperature, condition, humidity, wind, "
        "and an emoji icon. Always available — no API key needed."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name, e.g. 'Berlin' or 'San Francisco'"},
            "units": {"type": "string", "enum": ["celsius", "fahrenheit"], "default": "celsius"},
        },
        "required": ["city"],
    },
    invoke=weather,
    requires_auth=False,
    backend="builtin",
    version="1.0",
    tags=["weather", "free", "no-auth"],
))
