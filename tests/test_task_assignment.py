"""A task belongs to its assignees too: a parent puts a to-do on a
child's list, the child sees it, ticks it, and cannot delete it."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import login_client


@pytest.fixture
def family(fresh_app):
    from backend import spaces as S
    from backend.calendars import ensure_calendars_for_user
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    kid_c, kid = login_client(fresh_app, role="restricted", name="Yorik", email="k@example.local")
    other_c, other = login_client(fresh_app, role="restricted", name="Yarik", email="y@example.local")
    S.ensure_workspace_exists(beate, "Beate")
    for uid, name in ((beate, "Beate"), (kid, "Yorik"), (other, "Yarik")):
        S.ensure_personal_space(uid, name)
        ensure_calendars_for_user(uid, name)
        S.add_user_to_household(uid, "write" if uid == beate else "read")
    return {"beate": (beate_c, beate), "kid": (kid_c, kid), "other": (other_c, other)}


def _titles(client, role):
    r = client.get(f"/api/tasks?role={role}")
    assert r.status_code == 200, r.text
    return {t["title"] for t in r.json()}


def test_assign_in_the_app(family):
    beate_c, _ = family["beate"]; kid_c, kid = family["kid"]; other_c, _ = family["other"]
    r = beate_c.post("/api/tasks?role=member", json={"title": "Zimmer aufräumen", "assignee_user_ids": [kid]})
    assert r.status_code in (200, 201), r.text
    task_id = r.json()["id"]

    assert "Zimmer aufräumen" in _titles(kid_c, "restricted")
    assert "Zimmer aufräumen" not in _titles(other_c, "restricted")     # the brother's list stays his
    assert kid_c.patch(f"/api/tasks/{task_id}?role=restricted", json={"done": True}).status_code == 200
    assert kid_c.delete(f"/api/tasks/{task_id}?role=restricted").status_code == 403
    assert other_c.patch(f"/api/tasks/{task_id}?role=restricted", json={"done": False}).status_code in (403, 404)
    assert beate_c.delete(f"/api/tasks/{task_id}?role=member").status_code == 204


def test_assign_by_chat_and_calendar_view(family):
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.add_task.skill import execute
    _, beate = family["beate"]; kid_c, _ = family["kid"]
    asyncio.run(execute(ctx=SkillContext(Registry(), role="member", user_id=beate),
                        title="Müll rausbringen", person="Yorik"))
    assert "Müll rausbringen" in _titles(kid_c, "restricted")
    ids = ",".join(str(c["id"]) for c in kid_c.get("/api/calendars").json())
    r = kid_c.get(f"/api/tasks?role=restricted&calendar_ids={ids}")     # was int(uuid) → 500
    assert r.status_code == 200, r.text


def test_parent_ticks_a_childs_task_but_not_another_adults(family, fresh_app):
    from backend import spaces as S
    from backend.calendars import ensure_calendars_for_user
    beate_c, beate = family["beate"]; kid_c, kid = family["kid"]
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    S.ensure_personal_space(dirk, "Dirk"); ensure_calendars_for_user(dirk, "Dirk"); S.add_user_to_household(dirk, "write")

    # the child's own task, created by the child: a parent may tick it
    own = kid_c.post("/api/tasks?role=restricted", json={"title": "Ranzen packen"}).json()["id"]
    assert beate_c.patch(f"/api/tasks/{own}?role=member", json={"done": True}).status_code == 200
    # Beate puts one on Dirk's list; Dirk sees and ticks it
    r = beate_c.post("/api/tasks?role=member", json={"title": "Getränke holen", "assignee_user_ids": [dirk]})
    assert r.status_code in (200, 201), r.text
    assert "Getränke holen" in _titles(dirk_c, "platform_admin")
    # a task between adults is not the other adult's to change
    mine = dirk_c.post("/api/tasks?role=platform_admin", json={"title": "Steuer"}).json()["id"]
    assert beate_c.patch(f"/api/tasks/{mine}?role=member", json={"done": True}).status_code in (403, 404)
    # and a child is nobody's guardian
    assert kid_c.patch(f"/api/tasks/{mine}?role=restricted", json={"done": True}).status_code in (403, 404)
