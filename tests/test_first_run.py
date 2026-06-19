"""First-run setup contract — the path a new user takes from
`bash start.sh` to "the app is usable".

The README promises:
  1. Open localhost:8000 → first-run form to create admin account
  2. After submitting, the admin is logged in and the dock works
  3. Trying to log in again later uses /api/auth/login, not setup

That sequence is load-bearing — if first-run breaks, no one ever sees
the app. Pin the wire-level contract.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_fresh_install_advertises_setup_needed(fresh_app):
    """Before any password is set, /api/auth/me must distinguish
    "fresh install, run the setup flow" from "logged out by cookie
    expiry". The React shell renders the setup form when it sees
    setup_required=true."""
    client = TestClient(fresh_app)
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["logged_in"] is False
    assert body["setup_required"] is True


def test_setup_creates_admin_and_logs_in(fresh_app):
    """POST /api/auth/setup with email + password + name creates the
    admin user, sets the password, mints a session cookie, and the
    client is immediately logged in."""
    client = TestClient(fresh_app)
    r = client.post("/api/auth/setup", json={
        "email":    "founder@example.local",
        "password": "longenoughpw1",
        "name":     "Founder",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["user_id"] >= 1

    # New since the first-run-admin-provisioning change: setup ALWAYS
    # reports per-service provisioning state. With no bundled Paperless
    # or Immich configured in the test env, both should be marked
    # skipped (not errored) so the React shell can render
    # "External services not configured — set them up later if you want".
    assert "provisioning" in body
    for svc in ("paperless", "immich"):
        assert svc in body["provisioning"], f"missing {svc} state"
        st = body["provisioning"][svc]
        assert st["ok"] is False
        assert st.get("skipped") is True, (
            f"expected {svc} to be tagged skipped (admin token not "
            f"configured in test env), got: {st!r}"
        )

    # The setup response must set the session cookie so the client
    # doesn't have to do a follow-up login.
    assert any(c.name == "yorik_session" for c in client.cookies.jar), \
        "setup must set yorik_session cookie"

    # /api/auth/me now returns the new user.
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["logged_in"] is True
    assert me_body["user"]["email"] == "founder@example.local"
    assert me_body["user"]["role"]  == "admin"
    assert me_body["user"]["name"]  == "Founder"


def test_second_setup_is_refused(fresh_app):
    """Once any user has a password set, /api/auth/setup must 409 —
    new users go through the admin's user-management flow instead."""
    client = TestClient(fresh_app)
    r1 = client.post("/api/auth/setup", json={
        "email": "first@example.local",
        "password": "longenoughpw1",
        "name": "First",
    })
    assert r1.status_code == 200

    # Drop the session cookie so we're not piggybacking on the first
    # setup's auth — the 409 must trigger even for anonymous callers.
    client.cookies.clear()
    r2 = client.post("/api/auth/setup", json={
        "email": "second@example.local",
        "password": "longenoughpw2",
        "name": "Second",
    })
    assert r2.status_code == 409
    assert "setup already complete" in r2.json().get("detail", "")


def test_login_after_setup_works(fresh_app):
    """Run setup, log out, then log in with the same credentials —
    classic happy path the README promises."""
    client = TestClient(fresh_app)
    client.post("/api/auth/setup", json={
        "email":    "user@example.local",
        "password": "longenoughpw1",
        "name":     "User",
    })
    client.cookies.clear()

    r = client.post("/api/auth/login", json={
        "email":    "user@example.local",
        "password": "longenoughpw1",
    })
    assert r.status_code == 200
    assert any(c.name == "yorik_session" for c in client.cookies.jar)

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["logged_in"] is True
    assert me_body["user"]["email"] == "user@example.local"


def test_setup_provisions_admin_in_bundled_services(fresh_app, monkeypatch):
    """When bundled Paperless + Immich are reachable, /api/auth/setup
    should silently set the admin up in both — eliminating the manual
    'copy API token from Paperless web UI' dance for normal-people
    installs."""
    calls: dict[str, dict] = {}

    def fake_paperless(yorik_user_id, name, email, password):
        calls["paperless"] = {"uid": yorik_user_id, "email": email}
        return {"paperless_user_id": 7, "paperless_token": "tok",
                "paperless_username": "founder"}

    def fake_immich(yorik_user_id, name, email, password):
        calls["immich"] = {"uid": yorik_user_id, "email": email}
        return {"immich_user_id": "abc", "immich_api_key": "key"}

    from backend import external_users
    monkeypatch.setattr(external_users, "provision_paperless", fake_paperless)
    monkeypatch.setattr(external_users, "provision_immich",    fake_immich)

    client = TestClient(fresh_app)
    r = client.post("/api/auth/setup", json={
        "email": "founder@example.local",
        "password": "longenoughpw1",
        "name": "Founder",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["provisioning"]["paperless"] == {"ok": True}
    assert body["provisioning"]["immich"]    == {"ok": True}
    # Both functions were called with the admin's row.
    assert calls["paperless"]["email"] == "founder@example.local"
    assert calls["immich"]["email"]    == "founder@example.local"


def test_setup_survives_unexpected_provisioning_error(fresh_app, monkeypatch):
    """Bundled service is configured but returns garbage. Setup must
    still complete (admin is logged in) — the error surfaces in the
    response so the React shell can show 'Paperless connection failed
    — set up manually in Settings' but doesn't bail the whole flow."""
    def boom(yorik_user_id, name, email, password):
        raise RuntimeError("paperless: HTTP 500 from upstream")

    from backend import external_users
    monkeypatch.setattr(external_users, "provision_paperless", boom)
    # Leave immich as the unconfigured default — that path stays "skipped".

    client = TestClient(fresh_app)
    r = client.post("/api/auth/setup", json={
        "email": "founder@example.local",
        "password": "longenoughpw1",
        "name": "Founder",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    pap = body["provisioning"]["paperless"]
    assert pap["ok"] is False
    assert "skipped" not in pap, "real error should NOT be tagged skipped"
    assert "HTTP 500" in pap["error"]

    # Session cookie still set — admin is logged in.
    assert any(c.name == "yorik_session" for c in client.cookies.jar)


def test_login_with_wrong_password_is_rejected(fresh_app):
    client = TestClient(fresh_app)
    client.post("/api/auth/setup", json={
        "email": "user@example.local",
        "password": "longenoughpw1",
        "name": "User",
    })
    client.cookies.clear()
    r = client.post("/api/auth/login", json={
        "email":    "user@example.local",
        "password": "wrongpassword",
    })
    assert r.status_code in (401, 403)
    # No session minted.
    assert not any(c.name == "yorik_session" for c in client.cookies.jar)
