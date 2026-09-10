"""Reports on recordings: generation (LLM mocked), single notification,
the skill, task adoption reaching the named member's day plan."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import login_client, seed_user

LLM_ANSWER = {
    "summary": "Küche, Schule, Handwerker und der Samstag bei den Eltern.",
    "decisions": [{"text": "Samstag zu Beates Eltern", "who": "alle", "when": "2030-04-06 15:00"}],
    "tasks": [
        {"title": "Schule wegen Ausflug anrufen", "person": "Dirk", "due_date": "2030-04-04", "why": "seit einer Woche offen"},
        {"title": "Handwerker anrufen", "person": "Dirk", "due_date": "2030-04-05", "why": "hat nicht zurückgerufen"},
        {"title": "Küche aufräumen", "person": "Beate", "due_date": "nicht-ein-datum", "why": "Beate übernimmt"},
        "Mutter schreiben",
    ],
    "highlights": ["Das Abendessen war richtig gut."],
    "friction": ["Der Handwerker hat wieder nicht zurückgerufen."],
    "open_questions": [],
    "dates": [{"text": "Zahnarzt", "date": "2030-04-04", "time": "08:00"}],
}


@pytest.fixture
def transcript(fresh_app, monkeypatch, tmp_path):
    """A finished dinner recording (pipeline mocked) owned by Dirk with
    Beate at the table, plus a mocked report LLM."""
    import numpy as np
    from backend import recordings as R
    from backend import recording_reports as REP
    monkeypatch.setattr(R, "ROOT", tmp_path / "rec")
    monkeypatch.setattr(R, "decode_audio", lambda path: np.ones(5 * R.SAMPLE_RATE, dtype=np.float32))
    monkeypatch.setattr(R, "diarize", lambda audio: [{"start": 0.0, "end": 2.0, "cluster": 0}, {"start": 2.5, "end": 4.0, "cluster": 1}])
    monkeypatch.setattr(R, "identify_clusters", lambda audio, segs, cands: {})
    monkeypatch.setattr(R, "transcribe_segment", lambda audio: "Wer räumt die Küche auf?")
    monkeypatch.setattr(R, "schedule_processing", lambda rid: R._process_safe(rid))
    calls = []

    def fake_llm(messages):
        calls.append(messages)
        return json.loads(json.dumps(LLM_ANSWER))
    monkeypatch.setattr(REP, "_llm_json", fake_llm)
    from backend import spaces as _sp
    dirk = seed_user(name="Dirk", role="admin", email="dirk@example.local", password="pytestpw123", language="de")
    beate = seed_user(name="Beate", role="member", email="beate@example.local", password="pytestpw123")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate")):
        _sp.ensure_workspace_exists(uid, name)
        _sp.ensure_personal_space(uid, name)
    return {"dirk": dirk, "beate": beate, "llm": calls}


def _client(app, uid):
    from fastapi.testclient import TestClient
    from backend import auth_sessions
    c = TestClient(app)
    c.cookies.set(auth_sessions.COOKIE_NAME, auth_sessions.create_session(uid, user_agent="t", ip="127.0.0.1"))
    return c


def test_dinner_gets_report_and_one_notification(fresh_app, transcript):
    from backend import notifications as N
    from backend import recording_reports as REP
    dirk, beate = transcript["dirk"], transcript["beate"]
    c = _client(fresh_app, dirk)
    rid = c.post("/api/recordings", json={"title": "Abendessen", "kind": "dinner", "participants": [beate]}).json()["id"]
    c.post(f"/api/recordings/{rid}/chunk", data={"seq": "0"}, files={"audio": ("a.webm", b"x", "audio/webm")})
    assert c.post(f"/api/recordings/{rid}/finish").status_code == 200
    row = c.get(f"/api/recordings/{rid}").json()
    assert row["status"] == "done" and row["has_report"] is True and row["report_template"] == "dinner"

    rep = REP.get_report(rid)
    assert rep["summary"].startswith("Küche") and rep["template"] == "dinner"
    assert [t["title"] for t in rep["tasks"]] == ["Schule wegen Ausflug anrufen", "Handwerker anrufen", "Küche aufräumen", "Mutter schreiben"]
    assert rep["tasks"][2]["due_date"] == "" and rep["tasks"][3]["person"] == ""      # cleaned
    assert rep["dates"][0]["date"] == "2030-04-04" and rep["friction"] == ["Der Handwerker hat wieder nicht zurückgerufen."]
    # the prompt carried the transcript, the names and the date
    sys_msg, user_msg = transcript["llm"][0][0]["content"], transcript["llm"][0][1]["content"]
    assert "dinner" in sys_msg and "Participants: Dirk, Beate" in user_msg and "Sprecher 1: Wer räumt" in user_msg
    # one bell entry per person, for the report (not a second one for the transcript)
    for uid in (dirk, beate):
        titles = [n["title"] for n in N.list_for_user(uid)]
        assert titles == ["Abendessen: report ready"], titles
    assert N.list_for_user(beate)[0]["body"] == "4 tasks, 1 decision, 1 nice moment"


def test_conversation_kind_has_no_auto_report_but_skill_builds_it(fresh_app, transcript):
    from backend import notifications as N
    from backend import recording_reports as REP
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.recording_report.skill import execute as report
    dirk, beate = transcript["dirk"], transcript["beate"]
    c = _client(fresh_app, dirk)
    rid = c.post("/api/recordings", json={"kind": "conversation", "participants": [beate]}).json()["id"]
    c.post(f"/api/recordings/{rid}/chunk", data={"seq": "0"}, files={"audio": ("a.webm", b"x", "audio/webm")})
    c.post(f"/api/recordings/{rid}/finish")
    assert c.get(f"/api/recordings/{rid}").json()["has_report"] is False
    assert [n["title"] for n in N.list_for_user(beate)][0].endswith("transcript ready")

    bctx = SkillContext(Registry(), role="member", user_id=beate, conversation_id="c1")
    out = asyncio.run(report(bctx))                       # latest finished one, generated now
    assert out["recording_id"] == rid and out["generated"] is True and out["report"]["template"] == "meeting"
    assert len(transcript["llm"]) == 1
    out = asyncio.run(report(bctx, recording_id=rid))     # reused
    assert out["generated"] is False and len(transcript["llm"]) == 1
    out = asyncio.run(report(bctx, recording_id=rid, refresh=True, template="dinner"))
    assert out["generated"] is True and out["report"]["template"] == "dinner" and len(transcript["llm"]) == 2
    assert [n["title"] for n in N.list_for_user(beate)][0].endswith("report ready")   # from the first generation only
    assert len([n for n in N.list_for_user(beate) if n["title"].endswith("report ready")]) == 1
    assert REP.get_report(rid)["template"] == "dinner"

    stranger = SkillContext(Registry(), role="admin", user_id=seed_user(name="Stranger", role="admin"), conversation_id="c2")
    with pytest.raises(ValueError):
        asyncio.run(report(stranger, recording_id=rid))
    assert asyncio.run(report(stranger))["status"] == "none"


def test_adopted_task_reaches_the_named_persons_day_plan(fresh_app, transcript):
    from backend import day_plans as D
    from backend.calendars import ensure_calendars_for_user
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.add_task.skill import execute as add_task
    dirk, beate = transcript["dirk"], transcript["beate"]
    ensure_calendars_for_user(beate, "Beate")
    ctx = SkillContext(Registry(), role="admin", user_id=dirk, conversation_id="c1")
    out = asyncio.run(add_task(ctx, title="Küche aufräumen", person="beate", due_date="2030-04-04"))
    tid = out["task"]["id"] if isinstance(out.get("task"), dict) else out.get("task_id") or out.get("id")
    from backend.database import get_conn
    with get_conn() as conn:
        assigned = {str(r["user_id"]) for r in conn.execute("SELECT user_id FROM task_assignees WHERE task_id = ?", (tid,)).fetchall()}
    assert assigned == {dirk, beate}
    titles = {t["title"] for t in D.context_for(beate, "2030-04-04", "member")["open_tasks"]}
    assert "Küche aufräumen" in titles


def test_clean_unnests_json_strings_and_drops_junk():
    from backend import recording_reports as REP
    out = REP._clean({
        "summary": " x ",
        "decisions": ["Samstag zu den Eltern"],
        "tasks": '[{"title": "Küche", "person": "Sprecher 2", "due_date": "2026-09-10"}, {"title": ""}]',
        "highlights": '["schön"]',
        "friction": None,
        "open_questions": "nicht-json",
        "dates": [{"text": "Fußball", "date": "13.09."}],
    })
    assert out["summary"] == "x" and out["decisions"] == [{"text": "Samstag zu den Eltern", "who": "", "when": ""}]
    assert out["tasks"] == [{"title": "Küche", "person": "Sprecher 2", "due_date": "2026-09-10", "why": ""}]
    assert out["highlights"] == ["schön"] and out["friction"] == [] and out["open_questions"] == []
    assert out["dates"] == [{"text": "Fußball", "date": "", "time": ""}]


def test_repair_json_escapes_quotes_inside_strings():
    from backend import recording_reports as REP
    raw = '[{"title": "Küche", "why": "„Ich mache das" – am Abend gesagt."}, {"title": "Schule", "why": "seit "einer" Woche"}]'
    assert REP._loads(raw) == [{"title": "Küche", "why": "„Ich mache das\" – am Abend gesagt."},
                               {"title": "Schule", "why": "seit \"einer\" Woche"}]
    assert REP._extract_json('Here: ```json\n{"summary": "a "b" c", "tasks": []}\n```') == {"summary": "a \"b\" c", "tasks": []}
    assert REP._clean({"tasks": raw})["tasks"][0]["title"] == "Küche"
