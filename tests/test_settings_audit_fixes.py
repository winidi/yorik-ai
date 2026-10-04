"""Fixes from the settings audit of 2026-10-04: each setting does what
its screen says."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import login_client, seed_user


def _role(uid: str) -> str:
    from backend.database import get_conn
    with get_conn() as conn:
        return conn.execute("SELECT role FROM user_profiles WHERE id = ?", (uid,)).fetchone()["role"]


def test_role_change_stores_the_same_four_roles_as_create(fresh_app):
    admin_c, _ = login_client(fresh_app, role="admin", name="Anna")
    kid = seed_user(name="Kim", role="member")
    assert admin_c.patch(f"/api/users/{kid}", json={"role": "child"}).status_code == 200
    assert _role(kid) == "restricted"                       # "child" could not write anything
    assert admin_c.patch(f"/api/users/{kid}", json={"role": "member"}).status_code == 200
    assert admin_c.patch(f"/api/users/{kid}", json={"role": "restricted"}).status_code == 200
    assert _role(kid) == "restricted"


def test_a_household_admin_cannot_demote_the_owner(fresh_app):
    admin_c, _ = login_client(fresh_app, role="admin", name="Anna")
    owner = seed_user(name="Dirk", role="platform_admin")
    assert admin_c.patch(f"/api/users/{owner}", json={"role": "member"}).status_code == 403
    assert admin_c.delete(f"/api/users/{owner}").status_code == 403
    assert _role(owner) == "platform_admin"


def test_voice_enrollment_takes_uuids_and_only_your_own(fresh_app, monkeypatch):
    from backend import main as M
    member_c, me = login_client(fresh_app, role="member", name="Beate")
    other = seed_user(name="Dirk", role="admin")
    monkeypatch.setattr(M.voice_id, "enroll", lambda pid, path: {"id": pid, "enrolled": True})
    files = {"audio": ("enroll.webm", b"x" * 10, "audio/webm")}
    assert member_c.post(f"/api/voice-profile/{other}/enroll", files=files).status_code == 403
    r = member_c.post(f"/api/voice-profile/{me}/enroll", files=files)
    assert r.status_code == 200 and r.json()["id"] == me
    assert member_c.delete(f"/api/voice-profile/{other}/enrollment").status_code == 403
    # the list no longer hands every email to everyone
    assert [p["id"] for p in member_c.get("/api/voice-profiles").json()] == [me]


def test_voice_login_token_carries_a_uuid():
    from backend import voice_login_tokens as T
    uid = "0b5e4c1a-1111-2222-3333-444455556666"
    tok = T.mint(profile_id=uid, device_uuid="wall-1", source_sid="sid-1")
    assert T.verify(tok, expected_profile_id=uid, expected_device_uuid="wall-1", expected_source_sid="sid-1")


def test_the_app_switch_beats_the_install_default(fresh_app, monkeypatch):
    from backend import apps
    monkeypatch.setenv("YORIK_ENABLE_WHATSAPP", "1")
    assert apps._is_opt_in_enabled("whatsapp")              # nobody touched the switch: env default
    apps.set_opt_in_enabled("whatsapp", False)
    assert not apps._is_opt_in_enabled("whatsapp")          # "off" used to do nothing


def test_changing_your_password_keeps_you_signed_in_here(fresh_app):
    from fastapi.testclient import TestClient
    from backend import auth_sessions
    c, uid = login_client(fresh_app, role="member", name="Beate")
    other_sid = auth_sessions.create_session(uid, user_agent="phone", ip="127.0.0.1")
    r = c.post("/api/auth/change-password", json={"current_password": "pytestpw123", "new_password": "a-new-one-123"})
    assert r.status_code == 200
    assert c.get("/api/auth/me").json().get("logged_in") is not False
    phone = TestClient(fresh_app)
    phone.cookies.set(auth_sessions.COOKIE_NAME, other_sid)
    assert phone.get("/api/auth/me").json().get("logged_in") is False


def test_expired_sessions_are_not_listed_as_devices(fresh_app):
    from backend import auth_sessions
    from backend.database import get_conn
    c, uid = login_client(fresh_app, role="member", name="Beate")
    old = auth_sessions.create_session(uid, user_agent="old phone", ip="127.0.0.1")
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET expires_at = '2020-01-01T00:00:00' WHERE id = ?", (old,))
        conn.commit()
    ids = [d["id"] for d in c.get("/api/devices").json()]
    assert old not in ids and len(ids) == 1


def test_members_get_no_admin_details_from_system_status(fresh_app, monkeypatch):
    from backend import main as M
    monkeypatch.setattr(M, "_llm_reachable", lambda: True)
    member_c, _ = login_client(fresh_app, role="member", name="Beate")
    s = member_c.get("/api/system/status").json()
    assert s["llm"] == {"reachable": True} and s["backup"] is None and s["configured_connectors"] == []
    admin_c, _ = login_client(fresh_app, role="admin", name="Anna", email="anna@example.local")
    assert "base_url" in admin_c.get("/api/system/status").json()["llm"]
    assert member_c.get("/api/quality/summary").status_code == 403


def test_a_backup_without_the_database_is_a_failed_backup(fresh_app, monkeypatch, tmp_path):
    from backend import backup as B
    monkeypatch.setattr(B.credential_store, "get", lambda name: {"passphrase": "correct horse"})
    monkeypatch.setattr(B, "target_available", lambda p: {"available": True})
    monkeypatch.setattr(B, "get_config", lambda: {"target_path": str(tmp_path), "retain_count": 30})

    def bundle(cfg, staging: Path):
        archive = staging / "a.tar.gz"
        archive.write_bytes(b"photos only")
        return archive, ["immich_library"]
    monkeypatch.setattr(B, "_bundle", bundle)
    monkeypatch.setattr(B, "_encrypt_age", lambda src, dst, pw: dst.write_bytes(src.read_bytes()))
    r = B._run_backup_sync()
    assert r["ok"] is False and "database" in r["error"]
    assert B.list_history(limit=1)[0]["status"] == "failed"


def test_a_yearly_series_can_be_edited_after_the_reset(fresh_app):
    from datetime import datetime
    from backend.compose import series as S
    from backend.database import get_conn
    c, uid = login_client(fresh_app, role="member", name="Beate")
    s = S.create_series(kind="invoice", name="Invoices", scheme="R-{year}-{seq}", prefix="",
                        seq_padding=3, starting_number=1, year_reset=True, is_default=True,
                        owner_user_id=uid, notes=None)
    last_year = datetime.now().year - 1
    with get_conn() as conn:
        conn.execute("INSERT INTO document_series_allocations (series_id, number, formatted, year, document_kind) "
                     "VALUES (?, 47, 'R-x-047', ?, 'invoice')", (s["id"], last_year))
        conn.execute("UPDATE document_series SET next_number = 48, current_year = ? WHERE id = ?", (last_year, s["id"]))
        conn.commit()
    # a rename keeps the reset; a new number counts this year
    assert c.patch(f"/api/compose/series/{s['id']}", json={"name": "Bills out", "next_number": 48}).status_code == 200
    assert S.preview_next(s["id"])["number"] == 1
    assert c.patch(f"/api/compose/series/{s['id']}", json={"next_number": 4}).status_code == 200
    assert S.preview_next(s["id"])["number"] == 4
    # and only its owner sees its history
    other_c, _ = login_client(fresh_app, role="member", name="Dirk", email="dirk@example.local")
    assert other_c.get(f"/api/compose/series/{s['id']}/allocations").status_code == 404


def test_profile_changes_reach_the_letterhead_unless_edited_there(fresh_app):
    from backend.writing import letterhead as L
    c, uid = login_client(fresh_app, role="member", name="Beate")
    c.patch("/api/profile", json={"address_city": "Bonn", "phone": "0228 1"})
    lh = L.default_for(uid)                                      # copied from the profile
    L.update(lh["id"], uid, data={**lh["data"], "phone": "0228 999"})   # typed by hand
    assert c.patch("/api/profile", json={"address_city": "Köln", "phone": "0221 2"}).status_code == 200
    data = L.default_for(uid)["data"]
    assert data["city"] == "Köln" and data["phone"] == "0228 999"


def test_an_api_token_cannot_change_house_settings(fresh_app):
    from fastapi import HTTPException
    from backend import auth_sessions
    import pytest
    with pytest.raises(HTTPException) as e:
        auth_sessions.reject_api_token({"id": "x", "role": "admin", "auth": "api_token"})
    assert e.value.status_code == 403
    auth_sessions.reject_api_token({"id": "x", "role": "admin"})    # a browser session passes


def test_a_changed_thumb_replaces_the_earlier_vote(fresh_app):
    from backend.database import get_conn
    c, uid = login_client(fresh_app, role="member", name="Beate")
    for rating in (1, -1, -1):
        assert c.post("/api/feedback/turn", json={"conversation_id": "c1", "message_idx": 3, "rating": rating}).status_code == 201
    with get_conn() as conn:
        rows = conn.execute("SELECT rating FROM turn_feedback WHERE user_id = ?", (uid,)).fetchall()
    assert [r["rating"] for r in rows] == [-1]

