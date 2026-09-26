"""Join by QR (backend/member_invites.py): an admin invites a person,
the person's phone accepts with name, colour and PIN, and afterwards
opens Yorik with the PIN alone."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import login_client


@pytest.fixture
def app(fresh_app, monkeypatch):
    from backend import member_invites, tailscale_local
    # TestClient's address is "testclient", not an IP; the home-network
    # gate is tested on its own below.
    monkeypatch.setattr(member_invites, "is_trusted_lan_request", lambda r: True)
    monkeypatch.setattr(tailscale_local, "base_url", lambda: "https://yorik.example.ts.net")
    monkeypatch.setattr(tailscale_local, "join_page_url", lambda: None)
    monkeypatch.setattr(tailscale_local, "create_device_invite", lambda: {"ok": False, "reason": "not_configured"})
    # No photo/document archive in the test box.
    from backend import users
    orig = users.UserCreate
    monkeypatch.setattr(users, "UserCreate", lambda **kw: orig(**{**kw, "auto_provision": []}))
    return fresh_app


def _invite(admin, name="Mama", role="member"):
    r = admin.post("/api/invites", json={"name": name, "role": role, "color": "#e0486b"})
    assert r.status_code == 201, r.text
    body = r.json()
    token = body["join_url"].split("t=", 1)[1]
    return body, token


def test_admin_invite_points_at_the_tailnet_address(app):
    admin, _ = login_client(app, role="admin")
    body, token = _invite(admin)
    assert body["join_url"] == f"https://yorik.example.ts.net/r/join?t={token}"
    assert body["qr_url"] == body["join_url"]  # no public join page configured
    assert body["tailscale"] == "not_configured"


def test_member_cannot_invite(app):
    member, _ = login_client(app, role="member")
    assert member.post("/api/invites", json={"name": "X"}).status_code == 403


def test_accept_creates_member_device_and_session(app):
    admin, _ = login_client(app, role="admin")
    _, token = _invite(admin)
    phone = TestClient(app)
    prev = phone.get(f"/api/auth/invite/{token}")
    assert prev.status_code == 200 and prev.json()["name"] == "Mama"

    r = phone.post(f"/api/auth/invite/{token}/accept", json={"name": "Mama", "color": "#2f9e64", "pin": "4321"})
    assert r.status_code == 200, r.text
    me = phone.get("/api/auth/me").json()
    assert me["logged_in"] and me["user"]["name"] == "Mama"
    assert me["user"]["role"] == "member"
    assert me["user"]["onboarded_at"]  # the wizard isn't hers to answer
    assert phone.get("/api/auth/device").json()["known"] is True

    # single use
    again = TestClient(app).post(f"/api/auth/invite/{token}/accept", json={"name": "X", "pin": "1111"})
    assert again.status_code == 410


def test_device_login_with_pin(app):
    admin, _ = login_client(app, role="admin")
    _, token = _invite(admin)
    phone = TestClient(app)
    phone.post(f"/api/auth/invite/{token}/accept", json={"name": "Mama", "pin": "4321"})
    phone.post("/api/auth/logout")
    assert phone.get("/api/auth/me").json()["logged_in"] is False

    assert phone.post("/api/auth/device-login", json={"pin": "0000"}).status_code == 401
    assert phone.post("/api/auth/device-login", json={"pin": "4321"}).status_code == 200
    assert phone.get("/api/auth/me").json()["user"]["name"] == "Mama"


def test_device_login_needs_the_device_cookie(app):
    stranger = TestClient(app)
    assert stranger.post("/api/auth/device-login", json={"pin": "4321"}).status_code == 401


def test_kid_invite_makes_a_restricted_account(app):
    admin, _ = login_client(app, role="admin")
    _, token = _invite(admin, name="Lea", role="restricted")
    phone = TestClient(app)
    phone.post(f"/api/auth/invite/{token}/accept", json={"name": "Lea", "pin": "2468"})
    assert phone.get("/api/auth/me").json()["user"]["role"] == "restricted"


def test_revoked_and_bad_pin_are_refused(app):
    admin, _ = login_client(app, role="admin")
    body, token = _invite(admin)
    phone = TestClient(app)
    assert phone.post(f"/api/auth/invite/{token}/accept", json={"name": "M", "pin": "12"}).status_code == 400
    admin.delete(f"/api/invites/{body['id']}")
    assert phone.get(f"/api/auth/invite/{token}").status_code == 410


def test_joining_only_from_home(fresh_app, monkeypatch):
    from backend import member_invites
    monkeypatch.setattr(member_invites, "is_trusted_lan_request", lambda r: False)
    r = TestClient(fresh_app).get("/api/auth/invite/whatever")
    assert r.status_code == 403


def test_without_tailscale_the_invite_uses_the_address_in_use(fresh_app, monkeypatch):
    from backend import member_invites, tailscale_local
    monkeypatch.setattr(member_invites, "is_trusted_lan_request", lambda r: True)
    monkeypatch.setattr(tailscale_local, "base_url", lambda: None)
    monkeypatch.setattr(tailscale_local, "join_page_url", lambda: None)
    monkeypatch.setattr(tailscale_local, "create_device_invite", lambda: {"ok": False, "reason": "not_configured"})
    admin, _ = login_client(fresh_app, role="admin")
    body = admin.post("/api/invites", json={"name": "Oma"}, headers={"Host": "192.168.0.99:8000"}).json()
    assert body["join_url"].startswith("http://192.168.0.99:8000/r/join?t="), body
    assert body["home_only"] is True
    local = admin.post("/api/invites", json={"name": "Opa"}, headers={"Host": "localhost:8000"}).json()
    assert local["join_url"] is None and local["problem"]
