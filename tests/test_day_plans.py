"""Day planning: plan_day mechanics, undo, context, review."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import seed_user


def _ctx(uid, role="admin"):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=uid, conversation_id="c1")


@pytest.fixture
def planner(fresh_app):
    from backend import spaces as _sp
    from backend.calendars import ensure_calendars_for_user
    uid = seed_user(name="Planner", role="admin", email="plan@example.local")
    _sp.ensure_workspace_exists(uid, "Planner")
    _sp.ensure_personal_space(uid, "Planner")
    ensure_calendars_for_user(uid, "Planner")
    return uid


def _rows(sql, *params):
    from backend.database import get_conn
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def test_apply_plan_creates_tasks_and_blocks_idempotently(planner):
    from backend import day_plans as D
    uid = planner
    items = [
        {"key": "video", "title": "Video 9 schneiden", "start": "09:00", "end": "11:00", "estimated_minutes": 120},
        {"key": "steuer", "title": "Steuerberater anrufen"},
        {"key": "mails", "title": "Mails", "start": "14:00", "end": "14:30"},
    ]
    r = D.apply_plan(user_id=uid, plan_date="2030-04-01", items=items)
    assert r["summary"] == {"created": 3, "updated": 0, "removed": 0, "blocks": 2}
    tasks = _rows("SELECT id, title, plan_key, event_id, estimated_minutes FROM tasks WHERE plan_date='2030-04-01' ORDER BY id")
    assert [t["plan_key"] for t in tasks] == ["video", "steuer", "mails"]
    assert tasks[0]["event_id"] and tasks[1]["event_id"] is None and tasks[0]["estimated_minutes"] == 120
    ev = _rows("SELECT id, title, starts_at, ends_at, task_id, calendar_id FROM events WHERE plan_date='2030-04-01' ORDER BY starts_at")
    assert [e["starts_at"] for e in ev] == ["2030-04-01T09:00:00", "2030-04-01T14:00:00"]
    assert ev[0]["task_id"] == tasks[0]["id"]
    cal = _rows("SELECT kind, owner_user_id FROM calendars WHERE id=?", ev[0]["calendar_id"])[0]
    assert cal["kind"] == "plan" and str(cal["owner_user_id"]) == uid

    # revision: move the video block, drop mails, add a new task
    items2 = [
        {"key": "video", "title": "Video 9 schneiden", "start": "10:00", "end": "12:00"},
        {"key": "steuer", "title": "Steuerberater anrufen", "start": "12:15", "end": "12:30"},
        {"key": "sport", "title": "Laufen"},
    ]
    r2 = D.apply_plan(user_id=uid, plan_date="2030-04-01", items=items2)
    assert r2["summary"] == {"created": 1, "updated": 2, "removed": 1, "blocks": 2}
    tasks = _rows("SELECT title, plan_key FROM tasks WHERE plan_date='2030-04-01' ORDER BY id")
    assert [t["plan_key"] for t in tasks] == ["video", "steuer", "sport"]
    ev = _rows("SELECT plan_key, starts_at FROM events WHERE plan_date='2030-04-01' ORDER BY starts_at")
    assert [(e["plan_key"], e["starts_at"][11:16]) for e in ev] == [("video", "10:00"), ("steuer", "12:15")]
    assert _rows("SELECT status FROM day_plans WHERE user_id=? AND plan_date='2030-04-01'", uid)[0]["status"] == "applied"

    # undo the revision: back to the first plan
    D.rollback_plan(r2["rollback_args"])
    tasks = _rows("SELECT plan_key FROM tasks WHERE plan_date='2030-04-01' ORDER BY id")
    assert [t["plan_key"] for t in tasks] == ["video", "steuer", "mails"]
    ev = _rows("SELECT plan_key, starts_at FROM events WHERE plan_date='2030-04-01' ORDER BY starts_at")
    assert [(e["plan_key"], e["starts_at"][11:16]) for e in ev] == [("video", "09:00"), ("mails", "14:00")]


def test_validation(planner):
    from backend import day_plans as D
    with pytest.raises(ValueError):
        D.normalize_items("2030-04-01", [{"title": "x", "start": "10:00"}])
    with pytest.raises(ValueError):
        D.normalize_items("2030-04-01", [{"title": "x", "start": "11:00", "end": "10:00"}])
    with pytest.raises(ValueError):
        D.normalize_items("2030-04-01", [{"title": f"b{i}", "start": "10:00", "end": "11:00"} for i in range(7)])
    with pytest.raises(ValueError):
        D.normalize_items("2030-04-01", [{"title": "a", "key": "k"}, {"title": "b", "key": "k"}])
    assert D.normalize_items("2030-04-01", [{"title": "Müll raus!"}])[0]["key"] == "m-ll-raus"


def test_plan_day_skill_stages_one_undo_card(planner):
    from backend.skills.plan_day.skill import execute
    from backend import pending_actions as pa
    from backend.ui_tools import reset_ui_actions, get_ui_actions
    async def run():
        # ui actions live in a contextvar; read them inside the same task
        reset_ui_actions()
        out = await execute(_ctx(planner), date="2030-04-02",
                            items=[{"title": "Schreiben", "start": "08:00", "end": "10:00"}, {"title": "Einkaufen"}])
        return out, get_ui_actions()
    out, actions = asyncio.run(run())
    assert out["summary"]["created"] == 2 and out["pending_id"]
    cards = [a for a in actions if a["type"] == "pending_confirmation"]
    assert len(cards) == 1 and cards[0]["preview"]["date"] == "2030-04-02"
    pa.rollback(out["pending_id"])
    assert _rows("SELECT id FROM tasks WHERE plan_date='2030-04-02'") == []
    assert _rows("SELECT id FROM events WHERE plan_date='2030-04-02'") == []


def test_context_and_review(planner, monkeypatch):
    from backend import day_plans as D
    from backend.skills.plan_my_day.skill import execute as plan_my_day
    from backend.skills.day_review.skill import execute as day_review
    uid = planner
    # a fixed appointment, an old open task, yesterday's plan with a leftover
    cal = _rows("SELECT id FROM calendars WHERE owner_user_id=? AND kind='personal'", uid)[0]["id"]
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id) VALUES "
                     "('Zahnarzt', '2030-04-03T15:00:00', '2030-04-03T16:00:00', 0, ?, ?)", (cal, uid))
        conn.execute("INSERT INTO tasks (title, done, created_by_user_id, due_date) VALUES ('Alte Aufgabe', 0, ?, '2030-03-30')", (uid,))
        conn.commit()
    D.apply_plan(user_id=uid, plan_date="2030-04-02", items=[{"key": "rest", "title": "Rest von gestern"}, {"key": "done", "title": "Erledigt", "estimated_minutes": 30}])
    done_id = _rows("SELECT id FROM tasks WHERE plan_key='done'")[0]["id"]
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET done=1, done_at='2030-04-02T17:00:00', actual_minutes=45 WHERE id=?", (done_id,))
        conn.commit()
    rev = asyncio.run(day_review(_ctx(uid), date="2030-04-02"))
    assert rev["planned"] == 2 and rev["done_planned"] == 1 and rev["open"] == 1
    assert rev["estimated_minutes"] == 30 and rev["actual_minutes"] == 45 and rev["left"][0]["title"] == "Rest von gestern"

    # outside agent unreachable → planning still works and says so
    from backend.skills.ask_agent import skill as A
    async def down(ctx, question, context=None):
        return {"error": "Hermes is not reachable"}
    monkeypatch.setattr(A, "execute", down)
    out = asyncio.run(plan_my_day(_ctx(uid), date="2030-04-03"))
    c = out["context"]
    assert [e["title"] for e in c["fixed_events"]] == ["Zahnarzt"]
    titles = {t["title"] for t in c["open_tasks"]}
    assert {"Alte Aufgabe", "Rest von gestern"} <= titles
    assert [t["title"] for t in c["carry_over"]] == ["Rest von gestern"]
    assert c["yesterday_review"]["done_planned"] == 1
    assert "outside_error" in out and "outside_context" not in out
    assert _rows("SELECT status FROM day_plans WHERE plan_date='2030-04-03'")[0]["status"] == "draft"
