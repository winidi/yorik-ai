"""Scoped sharing: "Beate sees my tasks and calendar, not my documents"."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import login_client, seed_user


@pytest.fixture
def couple(fresh_app):
    from backend import spaces as S
    from backend.calendars import ensure_calendars_for_user
    from backend.database import get_conn
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    kid_c, kid = login_client(fresh_app, role="restricted", name="Kid", email="k@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate"), (kid, "Kid")):
        S.ensure_personal_space(uid, name)
        ensure_calendars_for_user(uid, name)
    with get_conn() as conn:
        for uid, title in ((dirk, "Dirks Aufgabe"), (beate, "Beates Aufgabe"), (kid, "Kids Aufgabe")):
            conn.execute("INSERT INTO tasks (title, done, created_by_user_id, space_id) VALUES (?, 0, ?, ?)",
                         (title, uid, S.personal_space_id(uid)))
        for uid, name in ((dirk, "Dirks Kontakt"), (beate, "Beates Kontakt")):
            conn.execute("INSERT INTO contacts (display_name, kind, status, created_by_user_id, space_id) "
                         "VALUES (?, 'person', 'active', ?, ?)", (name, uid, S.personal_space_id(uid)))
        conn.commit()
    return {"dirk": (dirk_c, dirk), "beate": (beate_c, beate), "kid": (kid_c, kid)}


def _tasks_seen(uid, role):
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.find_task_by_title.skill import execute
    out = asyncio.run(execute(ctx=SkillContext(Registry(), role=role, user_id=uid), query="Aufgabe"))
    return {m["title"] for m in out["matches"]}


def _contacts_seen(uid, role):
    from backend import contacts as C
    return {c["display_name"] for c in C.search("", role=role, user_id=uid, status="active")}


def test_nothing_is_shared_by_default(couple):
    _, dirk = couple["dirk"]; _, beate = couple["beate"]
    assert _tasks_seen(dirk, "platform_admin") == {"Dirks Aufgabe"}
    assert _tasks_seen(beate, "member") == {"Beates Aufgabe"}
    assert _contacts_seen(dirk, "platform_admin") == {"Dirks Kontakt"}


def test_share_tasks_only_and_take_it_back(couple):
    dirk_c, dirk = couple["dirk"]; beate_c, beate = couple["beate"]
    r = dirk_c.put(f"/api/sharing/{beate}", json={"areas": ["tasks", "calendar"], "level": "read"})
    assert r.status_code == 200, r.text
    assert _tasks_seen(beate, "member") == {"Beates Aufgabe", "Dirks Aufgabe"}
    assert _contacts_seen(beate, "member") == {"Beates Kontakt"}            # documents/contacts stay private
    assert _tasks_seen(dirk, "platform_admin") == {"Dirks Aufgabe"}          # one direction only

    status = beate_c.get("/api/sharing").json()
    dirk_row = next(m for m in status["members"] if m["user_id"] == dirk)
    assert dirk_row["shares_with_me"] == {"areas": ["tasks", "calendar"], "level": "read"}
    assert dirk_row["i_share"]["areas"] == []

    # read means read: Beate may not edit Dirk's task
    from backend import spaces as S
    from backend.database import get_conn
    with get_conn() as conn:
        row = dict(conn.execute("SELECT * FROM tasks WHERE title='Dirks Aufgabe'").fetchone())
    assert S.can_view_row(beate, "member", "tasks", row) is True
    assert S.can_write_row(beate, "member", "tasks", row) is False
    dirk_c.put(f"/api/sharing/{beate}", json={"areas": ["tasks"], "level": "write"})
    assert S.can_write_row(beate, "member", "tasks", row) is True

    assert dirk_c.put(f"/api/sharing/{beate}", json={"areas": [], "level": "read"}).status_code == 200
    assert _tasks_seen(beate, "member") == {"Beates Aufgabe"}


def test_everything_shared_is_a_plain_membership(couple):
    dirk_c, dirk = couple["dirk"]; _, beate = couple["beate"]
    dirk_c.put(f"/api/sharing/{beate}", json={"areas": ["tasks", "calendar", "contacts", "documents"], "level": "read"})
    assert _contacts_seen(beate, "member") == {"Beates Kontakt", "Dirks Kontakt"}
    from backend.database import get_conn
    with get_conn() as conn:
        assert conn.execute("SELECT scope FROM space_members WHERE user_id = ?", (beate,)).fetchone()["scope"] is None


def test_admin_sets_sharing_for_a_child_but_not_for_a_member(couple):
    dirk_c, dirk = couple["dirk"]; beate_c, beate = couple["beate"]; _, kid = couple["kid"]
    r = dirk_c.put(f"/api/sharing/{dirk}?owner={kid}", json={"areas": ["tasks"], "level": "read"})
    assert r.status_code == 200, r.text
    assert _tasks_seen(dirk, "platform_admin") == {"Dirks Aufgabe", "Kids Aufgabe"}
    assert dirk_c.put(f"/api/sharing/{dirk}?owner={beate}", json={"areas": ["tasks"], "level": "read"}).status_code == 403
    assert beate_c.put(f"/api/sharing/{beate}?owner={kid}", json={"areas": ["tasks"], "level": "read"}).status_code == 403
    assert dirk_c.put(f"/api/sharing/{beate}", json={"areas": ["photos"], "level": "read"}).status_code == 400
