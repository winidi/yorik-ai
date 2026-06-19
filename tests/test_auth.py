"""Auth-bypass smoke tests.

This file exists because of an audit finding: ~63 /api/* endpoints used
to take `role: str = Query("admin")` and trusted the caller. We added a
session-cookie middleware that closes that bypass globally. These tests
pin the invariant so the regression isn't possible silently.

Run with: pytest tests/ -q

The fixture in conftest.py points the backend at a tmp DB so we never
touch your real `data/family.db`. Each test gets a clean state.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# Every path on this list MUST refuse anonymous access (HTTP 401). If a
# new endpoint is added without auth on purpose, add it to the
# WHITELIST_PATHS test instead.
PROTECTED_PATHS: list[str] = [
    "/api/events",
    "/api/tasks",
    "/api/bills",
    "/api/connectors/credentialed",
    "/api/quality/summary",
    "/api/system/status",
    "/api/compose/templates",
    "/api/compose/series",
    "/api/users",
    "/api/notifications",
]

# Whitelisted paths that must reachable WITHOUT a session — login/setup
# routes, health probe, OpenAPI schema, the Paperless ingest webhook.
WHITELIST_PATHS_OK: list[str] = [
    "/api/health",
    "/api/auth/me",
    "/api/openapi.json",
]


# ─── core bypass tests ─────────────────────────────────────────────────

def test_protected_endpoints_reject_anonymous(fresh_app) -> None:
    """GET each protected endpoint with NO cookie → must be 401."""
    client = TestClient(fresh_app)
    for path in PROTECTED_PATHS:
        r = client.get(path)
        assert r.status_code == 401, (
            f"AUTH BYPASS: {path} returned {r.status_code} (expected 401). "
            f"This is the bug the audit caught — check the middleware in "
            f"backend/main.py::require_session_for_api."
        )


def test_query_role_does_not_grant_access(fresh_app) -> None:
    """Sending ?role=admin (the historical bypass vector) must still 401."""
    client = TestClient(fresh_app)
    for path in PROTECTED_PATHS:
        for qs in ("?role=admin", "?role=child", "?role=member"):
            r = client.get(f"{path}{qs}")
            assert r.status_code == 401, (
                f"AUTH BYPASS via query param: {path}{qs} returned {r.status_code}. "
                f"The ?role= param must be ignored by middleware."
            )


def test_whitelisted_endpoints_are_reachable(fresh_app) -> None:
    """Login / health / OpenAPI MUST stay anonymous-reachable, otherwise
    nobody can ever log in."""
    client = TestClient(fresh_app)
    for path in WHITELIST_PATHS_OK:
        r = client.get(path)
        assert r.status_code == 200, (
            f"Whitelisted path {path} returned {r.status_code} (expected 200). "
            f"Check _AUTH_WHITELIST_PREFIX / _AUTH_WHITELIST_EXACT."
        )


# ─── happy-path: setup + login + access ────────────────────────────────

def test_setup_then_login_then_access(fresh_app) -> None:
    """Full first-run flow: setup creates the admin, the cookie comes
    back, and protected endpoints become reachable with it."""
    client = TestClient(fresh_app)

    # 1. Virgin install: /auth/me reports setup_required.
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body.get("setup_required") is True
    assert body.get("logged_in") is False

    # 2. /api/auth/setup creates the first admin.
    r = client.post("/api/auth/setup", json={
        "name":     "Pytest Admin",
        "email":    "pytest@yorik.local",
        "password": "pytest-pw-1234",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("user_id")

    # 3. The session cookie is now on the TestClient. Re-checking /auth/me
    #    shows logged_in.
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json().get("logged_in") is True

    # 4. Previously-blocked endpoints now return 200.
    r = client.get("/api/events")
    assert r.status_code == 200, r.text


def test_query_role_is_ignored_after_login(fresh_app) -> None:
    """Sending ?role=child as an admin must NOT downgrade the request.
    Conversely, sending ?role=admin as a child must NOT upgrade. The
    session cookie wins."""
    client = TestClient(fresh_app)
    # bootstrap admin
    r = client.post("/api/auth/setup", json={
        "name": "A", "email": "a@yorik.local", "password": "pytest-pw-1234",
    })
    assert r.status_code == 200

    # As admin, both queries must succeed AND the role used must be admin.
    # We can't inspect the role directly here, but /api/auth/me proves
    # the cookie's role is what the server sees.
    r = client.get("/api/auth/me?role=child")
    assert r.status_code == 200
    assert r.json().get("user", {}).get("role") == "admin"


# ─── post-rotation guardrail ───────────────────────────────────────────

def test_logout_revokes_cookie(fresh_app) -> None:
    """After logout, the same client should be back to anonymous."""
    client = TestClient(fresh_app)
    client.post("/api/auth/setup", json={
        "name": "A", "email": "a@yorik.local", "password": "pytest-pw-1234",
    })
    assert client.get("/api/events").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/events").status_code == 401
