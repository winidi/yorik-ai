"""The wall shows the photos of the people who put them there, and a
photo proxy never falls back to the admin's Immich key (audit
docs/audits/2026-09-22-berechtigungen.md, package 12: 1.19, 4.4, 4.5)."""

from __future__ import annotations

from tests.conftest import login_client, seed_user


def test_only_consenting_people_are_on_the_photo_wall(fresh_app, monkeypatch):
    from backend import main as M
    from backend.database import get_conn
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    dirk = seed_user(name="Dirk", role="platform_admin", email="d@example.local", password="pytestpw123")
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET kiosk_agenda_consent = 1 WHERE id = ?", (dirk,)); conn.commit()
    assert M._kiosk_photo_people() == []                                          # agenda consent is not photo consent
    assert beate_c.get("/api/users/me/kiosk-photos-consent").json() == {"consent": False}
    assert beate_c.patch("/api/users/me/kiosk-photos-consent", json={"consent": True}).json() == {"consent": True}
    assert M._kiosk_photo_people() == [beate]


def test_photo_proxies_have_no_admin_fallback(fresh_app, monkeypatch):
    from backend import external_users as EU, credential_store as CS
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    monkeypatch.setattr(EU, "get_user_immich_creds", lambda uid: None)
    monkeypatch.setattr(CS, "get", lambda name: {"base_url": "http://i", "api_key": "admin-key"})   # present, must stay unused
    assert beate_c.get("/api/photos/abc/thumbnail").status_code == 503
    assert beate_c.get("/api/photos/abc/raw").status_code in (503, 404)
    assert beate_c.get("/api/photos/people/p1/thumbnail").status_code == 503
