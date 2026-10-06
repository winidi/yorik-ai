"""Diagnostics consent: off by default, the admin's to give from a
browser session, visible to everyone, the identity made only once it
is given; reports are listed to their own person."""

from __future__ import annotations

import json
import uuid


def test_consent_is_off_until_the_admin_says_yes(fresh_app):
    from tests.conftest import login_client
    admin_c, admin = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    member_c, member = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    r = member_c.get("/api/diagnostics/consent")
    assert r.status_code == 200
    assert r.json() == {**r.json(), "asked": False, "counts": False, "usage": False, "errors": False, "is_admin": False, "install_id": None}
    assert member_c.put("/api/diagnostics/consent", json={"counts": True}).status_code == 403
    me = admin_c.get("/api/auth/me").json()
    assert me["user"]["diag_consent_asked"] is False
    r = admin_c.put("/api/diagnostics/consent", json={"counts": True, "usage": False, "errors": True})
    assert r.status_code == 200 and r.json()["asked"] is True and r.json()["errors"] is True and r.json()["usage"] is False
    assert admin_c.get("/api/auth/me").json()["user"]["diag_consent_asked"] is True
    state = admin_c.get("/api/diagnostics/consent").json()
    assert state["install_id"] and len(state["install_id"]) == 36 and state["is_admin"] is True
    # "no" is an answer too: asked stays true, nothing is on
    admin_c.put("/api/diagnostics/consent", json={})
    state = admin_c.get("/api/diagnostics/consent").json()
    assert state["asked"] is True and not state["counts"] and not state["errors"]


def test_registry_and_reset_are_reachable(fresh_app):
    from tests.conftest import login_client
    admin_c, _ = login_client(fresh_app, role="admin", name="Anna", email="a@example.local")
    fields = admin_c.get("/api/diagnostics/registry").json()["fields"]
    assert any(f["path"] == "question.text" for f in fields) and all(f["purpose"] for f in fields)
    admin_c.put("/api/diagnostics/consent", json={"counts": True})
    first = admin_c.get("/api/diagnostics/consent").json()["install_id"]
    r = admin_c.post("/api/diagnostics/identity/reset")
    assert r.status_code == 200 and r.json()["install_id"] != first
    r = admin_c.get("/api/diagnostics/pseudonyms/resolve", params={"token": "person_1"})
    assert r.status_code == 200 and r.json()["matches"] == []


def test_reports_are_listed_to_their_own_person(fresh_app):
    from tests.conftest import login_client
    from backend.database import get_conn
    admin_c, admin = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    member_c, member = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    other_c, other = login_client(fresh_app, role="member", name="Jan", email="j@example.local")
    rid = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute("INSERT INTO diag_reports (id, user_id, kind, trigger, status, payload) VALUES (?, ?, 'error', 'thumbs_down', 'draft', ?)",
                     (rid, member, json.dumps({"kind": "error", "question": {"text": "wo ist person_1"}})))
        conn.commit()
    assert [r["id"] for r in member_c.get("/api/diagnostics/reports").json()["reports"]] == [rid]
    assert other_c.get("/api/diagnostics/reports").json()["reports"] == []
    assert other_c.get(f"/api/diagnostics/reports/{rid}").status_code == 403
    assert admin_c.get(f"/api/diagnostics/reports/{rid}").json()["payload"]["question"]["text"] == "wo ist person_1"
