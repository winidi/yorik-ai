"""Home checklist and per-person UI memory (backend/setup_checklist.py)."""
from tests.conftest import login_client


def test_admin_sees_family_and_backup_steps(fresh_app):
    admin, _ = login_client(fresh_app, role="admin")
    r = admin.get("/api/setup/checklist").json()
    ids = [s["id"] for s in r["steps"]]
    assert {"look", "ask", "phone", "family", "mail", "calendar", "backup"} <= set(ids)
    assert r["done"] == 0 and r["hidden"] is False


def test_child_gets_the_short_list(fresh_app):
    kid, _ = login_client(fresh_app, role="restricted")
    ids = [s["id"] for s in kid.get("/api/setup/checklist").json()["steps"]]
    assert ids == ["look", "ask", "phone"]


def test_ui_state_survives_and_merges(fresh_app):
    member, _ = login_client(fresh_app, role="member")
    assert member.get("/api/me/ui-state").json()["tour_done"] is False
    member.patch("/api/me/ui-state", json={"tour_done": True})
    member.patch("/api/me/ui-state", json={"hint_seen": "calendar"})
    st = member.get("/api/me/ui-state").json()
    assert st["tour_done"] is True and st["hints"] == ["calendar"]
    member.patch("/api/me/ui-state", json={"checklist_hidden": True})
    assert member.get("/api/setup/checklist").json()["hidden"] is True
