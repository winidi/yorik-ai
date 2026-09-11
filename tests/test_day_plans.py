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


def test_context_backlog_candidates_rules_and_report_ref(planner, monkeypatch):
    """The planner sees the whole picture: free minutes, backlog counts,
    un-adopted report tasks as candidates, the person's rules; a plan item
    with report_ref adopts the proposal."""
    import json
    from backend import day_plans as D
    from backend.database import get_conn
    from backend.skills.plan_my_day.skill import parse_candidates
    from backend.skills.remember_planning_rule.skill import execute as remember
    uid = planner
    cal = _rows("SELECT id FROM calendars WHERE owner_user_id=? AND kind='personal'", uid)[0]["id"]
    with get_conn() as conn:
        conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id) VALUES "
                     "('Zahnarzt', '2030-04-03T15:00:00', '2030-04-03T16:30:00', 0, ?, ?)", (cal, uid))
        conn.execute("INSERT INTO tasks (title, done, created_by_user_id, due_date) VALUES ('Später', 0, ?, '2030-04-20')", (uid,))
        conn.execute("INSERT INTO tasks (title, done, created_by_user_id) VALUES ('Undatiert', 0, ?)", (uid,))
        rep = {"summary": "s", "decisions": [], "highlights": [], "friction": [], "open_questions": [], "dates": [],
               "tasks": [{"title": "Glasflaschen wegbringen", "person": "Sprecher 2", "due_date": "", "why": "Keller"},
                         {"title": "Schon erledigt", "person": "", "due_date": "", "why": "", "task_id": 1}]}
        conn.execute("INSERT INTO recordings (owner_user_id, title, kind, status, started_at, report_json) VALUES (?, 'Abendessen', 'dinner', 'done', '2030-04-01 19:00:00', ?)",
                     (uid, json.dumps(rep)))
        rid = conn.execute("SELECT max(id) AS id FROM recordings").fetchone()["id"]
        conn.commit()
    asyncio.run(remember(_ctx(uid), rule="Küche macht Beate."))
    asyncio.run(remember(_ctx(uid), rule="küche macht beate."))       # no duplicate
    c = D.context_for(uid, "2030-04-03", "admin")
    assert c["free_minutes"] == 12 * 60 - 90
    assert c["backlog"]["open_total"] == 2 and [t["title"] for t in c["backlog"]["due_later"]] == ["Später"]
    assert [t["title"] for t in c["open_tasks"]] == ["Undatiert"]
    assert c["report_candidates"] == [{"report_ref": f"{rid}:0", "title": "Glasflaschen wegbringen", "person": "Sprecher 2",
                                       "due_date": "", "why": "Keller", "from": "Abendessen 2030-04-01"}]
    assert c["rules"] == "Küche macht Beate."
    assert parse_candidates("- Video 9 schneiden | 90 | Deadline Freitag\n- Steuerberater anrufen | 15min | seit Wochen offen\nkein Punkt") == [
        {"title": "Video 9 schneiden", "estimated_minutes": 90, "why": "Deadline Freitag"},
        {"title": "Steuerberater anrufen", "estimated_minutes": 15, "why": "seit Wochen offen"}]

    D.apply_plan(user_id=uid, plan_date="2030-04-03", items=[
        {"key": "glas", "title": "Glasflaschen wegbringen", "report_ref": f"{rid}:0"},
        {"key": "undatiert", "title": "Undatiert", "task_id": _rows("SELECT id FROM tasks WHERE title='Undatiert'")[0]["id"]},
    ])
    rep2 = json.loads(_rows("SELECT report_json FROM recordings WHERE id=?", rid)[0]["report_json"])
    tid = rep2["tasks"][0]["task_id"]
    assert tid and rep2["tasks"][0]["adopted_by"] == "Planner"
    assert {str(r["user_id"]) for r in _rows("SELECT user_id FROM task_assignees WHERE task_id=?", tid)} == {uid}
    assert D.context_for(uid, "2030-04-03", "admin")["report_candidates"] == []   # adopted, no longer a candidate


def test_planning_rules_routes(fresh_app):
    from tests.conftest import login_client
    client, uid = login_client(fresh_app, role="member", name="Beate")
    assert client.get("/api/profile/planning-rules").json() == {"rules": ""}
    assert client.patch("/api/profile/planning-rules", json={"rules": "  Einkauf mache ich.  "}).json() == {"rules": "Einkauf mache ich."}
    assert client.get("/api/profile/planning-rules").json()["rules"] == "Einkauf mache ich."


def test_agent_candidates_skip_what_yorik_already_has(planner, monkeypatch):
    from backend.database import get_conn
    from backend.skills.plan_my_day.skill import execute as plan_my_day
    uid = planner
    with get_conn() as conn:
        conn.execute("INSERT INTO tasks (title, done, created_by_user_id) VALUES ('Steuerberater anrufen', 0, ?)", (uid,))
        conn.commit()
    seen = {}
    ctx = _ctx(uid)
    async def fake_call(name, **kw):
        assert name == "ask_agent"
        seen["q"] = kw["question"]
        return {"answer": "- Steuerberater anrufen | 15 | offen\n- Video 9 schneiden | 90 | Deadline"}
    ctx.call_skill = fake_call
    out = asyncio.run(plan_my_day(ctx, date="2030-04-03"))
    assert "Homebase" in seen["q"] and "keine Yorik-Tools" in seen["q"]
    assert [c["title"] for c in out["agent_candidates"]] == ["Video 9 schneiden"]
