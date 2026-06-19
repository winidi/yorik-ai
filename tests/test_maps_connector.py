"""Maps connector tests — mock requests.get/post so we don't hit
Nominatim / OSRM / Overpass for real.

Pins the response-shape contract that the calculate_travel_time,
find_provider_nearby, and navigate_to skills rely on.
"""

from __future__ import annotations

import pytest


class _StubResponse:
    """Minimal stand-in for requests.Response — only the surface the
    maps connector actually uses."""

    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text or (str(payload)[:500])

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} stubbed")


@pytest.fixture
def maps(monkeypatch):
    """Import the connector with a clean geocode cache so tests don't
    bleed across each other."""
    from backend.connectors import maps as m
    m._GEOCODE_CACHE.clear()
    # Avoid the 1s Nominatim politeness sleep inside `directions`.
    monkeypatch.setattr(m.time, "sleep", lambda _: None)
    return m


def _route_request(method, url_payloads):
    """Build a fake requests.get / requests.post that picks the right
    stubbed payload by URL substring."""
    def fake(url, *args, **kwargs):
        for substring, response in url_payloads.items():
            if substring in url:
                return response
        raise AssertionError(f"unexpected {method.upper()} to {url}")
    return fake


# ───────────────────────── geocode ─────────────────────────

def test_geocode_returns_canonical_shape(maps, monkeypatch):
    nominatim_payload = [{
        "display_name": "Hannover, Niedersachsen, Deutschland",
        "lat":          "52.3186",
        "lon":          "10.2333",
        "type":         "city",
    }]
    monkeypatch.setattr(maps.requests, "get",
                        _route_request("get",
                                       {"nominatim.openstreetmap.org":
                                        _StubResponse(nominatim_payload)}))

    out = maps.maps(op="geocode", query="Hannover")
    assert out["op"] == "geocode"
    assert out["lat"] == pytest.approx(52.3186)
    assert out["lon"] == pytest.approx(10.2333)
    assert "Hannover" in out["label"]


def test_geocode_missing_query_errors(maps):
    out = maps.maps(op="geocode")
    assert out["ok"] is False
    assert "query" in out["error"]


def test_geocode_no_match(maps, monkeypatch):
    monkeypatch.setattr(maps.requests, "get",
                        _route_request("get",
                                       {"nominatim.openstreetmap.org":
                                        _StubResponse([])}))
    out = maps.maps(op="geocode", query="zzzzz nowhere")
    assert out["ok"] is False
    assert "no match" in out["error"]


def test_geocode_result_is_cached(maps, monkeypatch):
    calls = {"n": 0}
    def counting(url, *a, **kw):
        calls["n"] += 1
        return _StubResponse([{
            "display_name": "Berlin",
            "lat": "52.52", "lon": "13.405", "type": "city",
        }])
    monkeypatch.setattr(maps.requests, "get", counting)

    maps.maps(op="geocode", query="Berlin")
    maps.maps(op="geocode", query="Berlin")  # second call — cache hit
    maps.maps(op="geocode", query="BERLIN")  # case-insensitive cache key
    assert calls["n"] == 1, "expected the Nominatim call to be cached"


# ───────────────────────── directions ─────────────────────────

def test_directions_returns_canonical_shape(maps, monkeypatch):
    """Geocode A, geocode B, OSRM call → distance_km + duration_min."""
    nominatim_a = [{"display_name": "Hamburg", "lat": "53.55", "lon": "9.99",
                    "type": "city"}]
    nominatim_b = [{"display_name": "Berlin",  "lat": "52.52", "lon": "13.405",
                    "type": "city"}]
    osrm_payload = {"routes": [{"distance": 289_000, "duration": 10_800}]}

    nominatim_responses = iter([_StubResponse(nominatim_a),
                                _StubResponse(nominatim_b)])

    def fake_get(url, *args, **kwargs):
        if "nominatim" in url:
            return next(nominatim_responses)
        if "router.project-osrm.org" in url:
            return _StubResponse(osrm_payload)
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(maps.requests, "get", fake_get)
    # Force OSRM path (no ORS key configured).
    monkeypatch.setattr(maps, "_ors_key", lambda: None)

    out = maps.maps(op="directions", **{"from": "Hamburg", "to": "Berlin"})
    assert out["op"]            == "directions"
    assert out["distance_km"]   == 289.0
    assert out["duration_min"]  == 180        # 10800s / 60
    assert out["duration_human"] == "3h 0m"
    assert out["provider"]      == "osrm"
    assert "OSRM" in out["source"]


def test_directions_missing_endpoints_errors(maps):
    out = maps.maps(op="directions", **{"from": "", "to": "x"})
    assert out["ok"] is False
    assert "from" in out["error"]


def test_directions_falls_through_when_ors_fails(maps, monkeypatch):
    """ORS configured but returns non-200 → fall through to OSRM rather
    than failing the call. Matches the policy comment in _route()."""
    nominatim_a = [{"display_name": "A", "lat": "1", "lon": "2", "type": "x"}]
    nominatim_b = [{"display_name": "B", "lat": "3", "lon": "4", "type": "x"}]
    osrm_payload = {"routes": [{"distance": 1234, "duration": 60}]}
    nominatim_responses = iter([_StubResponse(nominatim_a),
                                _StubResponse(nominatim_b)])

    def fake_get(url, *a, **kw):
        if "nominatim" in url:
            return next(nominatim_responses)
        if "router.project-osrm.org" in url:
            return _StubResponse(osrm_payload)
        raise AssertionError(url)
    def fake_post(url, *a, **kw):
        if "openrouteservice.org" in url:
            return _StubResponse({"error": "quota"}, status=429,
                                 text="rate limited")
        raise AssertionError(url)

    monkeypatch.setattr(maps.requests, "get",  fake_get)
    monkeypatch.setattr(maps.requests, "post", fake_post)
    monkeypatch.setattr(maps, "_ors_key", lambda: "fake-key")

    out = maps.maps(op="directions", **{"from": "A", "to": "B"})
    assert out["provider"] == "osrm", "should fall through to OSRM"
    assert out["distance_m"] == 1234


# ───────────────────────── unknown op ─────────────────────────

def test_unknown_op_returns_help(maps):
    out = maps.maps(op="teleport", query="x")
    assert out["ok"] is False
    assert "unknown op" in out["error"]
    assert "geocode" in out["error"]


def test_test_connection_up(maps, monkeypatch):
    monkeypatch.setattr(maps.requests, "get",
                        _route_request("get",
                                       {"nominatim.openstreetmap.org":
                                        _StubResponse([])}))
    monkeypatch.setattr(maps, "_ors_key", lambda: None)
    out = maps.maps(op="test_connection")
    assert out["ok"] is True
    assert out["nominatim"] == "up"
    assert out["routing_provider"] == "osrm"
    assert out["ors_configured"] is False
