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


def test_timer_on_an_assigned_task_and_in_the_board_feed(family):
    beate_c, _ = family["beate"]; kid_c, kid = family["kid"]
    a = beate_c.post("/api/tasks?role=member", json={"title": "Vokabeln", "assignee_user_ids": [kid]}).json()["id"]
    b = beate_c.post("/api/tasks?role=member", json={"title": "Mathe", "assignee_user_ids": [kid]}).json()["id"]
    assert kid_c.post(f"/api/tasks/{a}/start?role=restricted").status_code == 200      # the child times its own to-do
    r = beate_c.post(f"/api/tasks/{b}/start?role=member")                              # a parent starts the next one
    assert r.status_code == 200 and r.json()["started_at"]
    rows = {t["id"]: t for t in kid_c.get("/api/tasks?role=restricted").json()}
    assert rows[a]["started_at"] is None and rows[b]["started_at"]                     # one timer per person
    kid_c.patch("/api/users/me/kiosk-agenda-consent", json={"consent": True})
    feed = kid_c.get("/api/ambient/board").json()
    card = next(t for t in feed["tasks"] if t["id"] == b)
    assert card["started_at"] and "actual_minutes" in card
    assert kid_c.post(f"/api/tasks/{b}/stop?role=restricted").json()["started_at"] is None


def test_board_column_order_is_per_person_and_parents_sort_for_children(family, fresh_app):
    from backend.database import get_conn
    beate_c, beate = family["beate"]; kid_c, kid = family["kid"]; other_c, other = family["other"]
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET kiosk_agenda_consent = 1"); conn.commit()
    a = beate_c.post("/api/tasks?role=member", json={"title": "Zimmer", "assignee_user_ids": [kid]}).json()["id"]
    b = beate_c.post("/api/tasks?role=member", json={"title": "Lesen", "assignee_user_ids": [kid, other]}).json()["id"]
    hers = beate_c.post("/api/tasks?role=member", json={"title": "Steuer"}).json()["id"]

    # the child sorts its own column; a task that is not in it is dropped
    r = kid_c.put("/api/ambient/board/order", json={"user_id": kid, "task_ids": [b, a, hers]})
    assert r.status_code == 200 and r.json()["task_ids"] == [b, a]
    feed = {t["id"]: t for t in kid_c.get("/api/ambient/board").json()["tasks"]}
    assert feed[b]["positions"] == {kid: 0} and feed[a]["positions"] == {kid: 1}      # Yarik's column untouched
    # a parent sorts a child's column, a child nobody else's, an adult not another adult's
    assert beate_c.put("/api/ambient/board/order", json={"user_id": kid, "task_ids": [a, b]}).status_code == 200
    assert kid_c.put("/api/ambient/board/order", json={"user_id": other, "task_ids": [b]}).status_code == 403
    assert kid_c.put("/api/ambient/board/order", json={"user_id": beate, "task_ids": [hers]}).status_code == 403
    feed = {t["id"]: t for t in kid_c.get("/api/ambient/board").json()["tasks"]}
    assert feed[a]["positions"] == {kid: 0} and feed[hers]["positions"] == {}
    # rewording a child's to-do is the ordinary task PATCH
    assert beate_c.patch(f"/api/tasks/{a}?role=member", json={"title": "Zimmer aufräumen"}).status_code == 200
    assert kid_c.patch(f"/api/tasks/{a}?role=restricted", json={"title": "Zimmer (später)"}).status_code == 200
