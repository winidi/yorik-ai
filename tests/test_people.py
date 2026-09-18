"""People: a colour and a photo per household member, visible to the
household, following into the personal calendar and the kiosk picker."""

from __future__ import annotations

import io

import pytest

from tests.conftest import login_client, seed_user


def _png(w=40, h=60, color=(200, 30, 90)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def avatars(monkeypatch, tmp_path):
    from backend import people as P
    monkeypatch.setattr(P, "AVATAR_DIR", tmp_path / "avatars")
    return tmp_path / "avatars"


def test_colour_and_photo(fresh_app, avatars):
    from backend import spaces as _sp
    from backend.calendars import ensure_calendars_for_user
    from backend.database import get_conn
    client, uid = login_client(fresh_app, role="member", name="Beate")
    _sp.ensure_workspace_exists(uid, "Beate"); _sp.ensure_personal_space(uid, "Beate"); ensure_calendars_for_user(uid, "Beate")
    other = seed_user(name="Dirk", role="admin", password="pytestpw123")

    look = client.get("/api/profile/look").json()
    assert look["color"].startswith("#") and look["avatar_url"] is None and len(look["palette"]) == 8
    assert client.patch("/api/profile/look", json={"color": "red"}).status_code == 400
    look = client.patch("/api/profile/look", json={"color": "#E0486B"}).json()
    assert look["color"] == "#e0486b"
    with get_conn() as conn:
        cal = conn.execute("SELECT color FROM calendars WHERE owner_user_id=? AND kind='personal'", (uid,)).fetchone()
    assert cal["color"] == "#e0486b"                                   # the personal calendar follows

    # photo: any image, centre-cropped to a 256 px square JPEG
    r = client.post("/api/profile/avatar", files={"image": ("me.png", _png(), "image/png")})
    assert r.status_code == 200 and r.json()["avatar_url"].startswith(f"/api/users/{uid}/avatar?v=")
    from PIL import Image
    img = Image.open(avatars / f"{uid}.jpg")
    assert img.size == (256, 256) and img.format == "JPEG"
    assert client.post("/api/profile/avatar", files={"image": ("x.txt", b"not an image", "text/plain")}).status_code == 400

    # the household sees colour and photo; another member can load the photo
    people = {p["name"]: p for p in client.get("/api/people/household").json()["people"]}
    assert people["Beate"]["color"] == "#e0486b" and people["Beate"]["avatar_url"]
    assert people["Dirk"]["color"].startswith("#") and people["Dirk"]["avatar_url"] is None
    from fastapi.testclient import TestClient
    from backend import auth_sessions
    d = TestClient(fresh_app)
    d.cookies.set(auth_sessions.COOKIE_NAME, auth_sessions.create_session(other, user_agent="t", ip="127.0.0.1"))
    r = d.get(people["Beate"]["avatar_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert d.get(f"/api/users/{other}/avatar").status_code == 404
    assert TestClient(fresh_app).get(people["Beate"]["avatar_url"]).status_code == 401   # no session, no photo

    assert client.delete("/api/profile/avatar").json()["avatar_url"] is None
    assert not (avatars / f"{uid}.jpg").exists()


def test_default_colours_are_distinct_and_stable(fresh_app):
    from backend import people as P
    a = seed_user(name="A", role="admin"); b = seed_user(name="B", role="member"); c = seed_user(name="C", role="member")
    cols = [p["color"] for p in P.household()]
    assert len(cols) == len(set(cols)) == 3
    assert P.color_for(a, None) == P.household()[0]["color"]
