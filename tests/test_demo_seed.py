"""End-to-end test for the demo-seed feature.

The README, the docs/screenshots/README.md, and the Home-app
"Try with example data" button all promise that POST /api/demo/seed
just works. This test pins that promise.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _logged_in_client(app, role: str = "admin"):
    """Mint a real session for a user of the given role."""
    from backend import auth_sessions
    from backend.database import DEFAULT_DB_PATH, conn_ctx
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO user_profiles "
            "(name, email, role, voice_id, password_hash, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"{role}-user", f"{role}@example.local", role, f"vid-{role}",
             auth_sessions.hash_password("pytestpw123"), "en"),
        )
        uid = cur.lastrowid
        conn.commit()
    sid = auth_sessions.create_session(uid, user_agent="pytest", ip="127.0.0.1")
    client = TestClient(app)
    client.cookies.set(auth_sessions.COOKIE_NAME, sid)
    return client


def test_seed_status_then_seed_then_remove(fresh_app):
    client = _logged_in_client(fresh_app, role="admin")

    # Fresh DB → nothing seeded.
    r = client.get("/api/demo/status")
    assert r.status_code == 200
    assert r.json().get("seeded") is False

    # Seed it.
    r = client.post("/api/demo/seed")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    inserted = body["inserted"]
    # Sanity: each of the categories the README + screenshots docs
    # promise should have at least one row.
    assert inserted.get("events", 0)        >= 1
    assert inserted.get("tasks", 0)         >= 1
    assert inserted.get("bills", 0)         >= 1

    # Status now reflects the seed.
    r = client.get("/api/demo/status")
    assert r.status_code == 200
    s = r.json()
    assert s.get("seeded") is True
    assert s.get("seeded_at"), "expected a timestamp on the seed record"

    # Second seed → 409 (the contract the React DemoDataPanel relies on
    # to render "already loaded" instead of duplicating).
    r = client.post("/api/demo/seed")
    assert r.status_code == 409

    # Remove → counts come back, status flips.
    r = client.delete("/api/demo")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.get("/api/demo/status")
    assert r.json().get("seeded") is False

    # Remove-when-empty is idempotent (returns 200, not 404).
    r = client.delete("/api/demo")
    assert r.status_code == 200


def test_non_admin_cannot_seed(fresh_app):
    """The mutation endpoints require admin — a child/member running
    the demo button should get a 403, not silently mutate the DB."""
    client = _logged_in_client(fresh_app, role="child")
    r = client.post("/api/demo/seed")
    assert r.status_code == 403


def test_status_is_readable_by_non_admin(fresh_app):
    """The Home-app placement logic checks status from whichever user
    is logged in — read access must NOT be gated to admin only."""
    client = _logged_in_client(fresh_app, role="member")
    r = client.get("/api/demo/status")
    assert r.status_code == 200
