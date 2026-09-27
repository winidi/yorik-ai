"""pipeline skill: "fass da nach, wenn keine Antwort kommt" from the chat
(Dirk 2026-09-27), plus list / pause / resume."""

from __future__ import annotations

import asyncio
import json

from tests.test_pipelines import _account, _mail, _utc, home  # noqa: F401 — fixture


def _ctx(uid, role="platform_admin"):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=uid)


def _run(uid, monkeypatch, **kw):
    from backend.pipelines import routes
    from backend.skills.pipeline.skill import execute
    from backend.ui_tools import _pending_ui_actions
    monkeypatch.setattr(routes, "draft_with_llm", lambda pid, owner: None)

    async def go():
        _pending_ui_actions.set([])
        out = await execute(ctx=_ctx(uid), **kw)
        return out, _pending_ui_actions.get()
    return asyncio.run(go())


def test_follow_up_of_the_mail_just_sent(home, monkeypatch):
    sent = _mail(home["dirk"], home["acct"], sent=True, frm="dirk@example.org", to=["illtys@gmx.de"],
                 subject="Test von Yorik", received=_utc(hours_ago=1))
    out, actions = _run(home["dirk"], monkeypatch, op="follow_up", to="illtys@gmx.de")
    assert out["ok"] is True and out["title"] == "Test von Yorik"
    card = [a for a in actions if a["type"] == "pipeline_ready"][0]
    assert card["link"] == f"/r/pipelines/{out['pipeline_id']}" and card["to"] == ["illtys@gmx.de"]
    from backend.pipelines import store
    p = store.get(out["pipeline_id"], home["dirk"])
    assert p["state"] == "entwurf" and p["origin"]["to"] == ["illtys@gmx.de"]
    assert sent


def test_prepared_but_not_sent_says_so(home, monkeypatch):
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)",
                     (f"pending_email_draft_{home['dirk']}", json.dumps({"to": "illtys@gmx.de"})))
        conn.commit()
    out, actions = _run(home["dirk"], monkeypatch, op="follow_up", to="illtys@gmx.de")
    assert out["ok"] is False and "not sent yet" in out["_llm_hint"]
    assert not actions


def test_list_pause_resume(home, monkeypatch):
    from backend.pipelines import store
    out, _ = _run(home["dirk"], monkeypatch, op="follow_up", subject="Kündigung Stromvertrag")
    pid = out["pipeline_id"]
    listed, _ = _run(home["dirk"], monkeypatch, op="list")
    assert [p["title"] for p in listed["pipelines"]] == ["Kündigung Stromvertrag"]
    store.update(pid, state="laeuft")
    paused, _ = _run(home["dirk"], monkeypatch, op="pause", pipeline_id=pid)
    assert paused["state"] == "pausiert"
    resumed, _ = _run(home["dirk"], monkeypatch, op="resume", pipeline_id=pid)
    assert resumed["state"] == "laeuft"


def test_someone_elses_pipeline_is_not_touched(home, monkeypatch):
    out, _ = _run(home["dirk"], monkeypatch, op="follow_up", subject="Kündigung Stromvertrag")
    other, _ = _run(home["beate"], monkeypatch, op="pause", pipeline_id=out["pipeline_id"])
    assert other["ok"] is False and "not found" in other["_llm_hint"]


def test_universal_search_finds_the_pipeline(home, monkeypatch):
    from backend import search_routes
    _run(home["dirk"], monkeypatch, op="follow_up", subject="Kündigung Stromvertrag")
    hits = search_routes._search_pipelines("was ist mit der stromvertrag sache", home["dirk"])
    assert [h["title"] for h in hits] == ["Kündigung Stromvertrag"] and hits[0]["subtitle"] == "Entwurf"
    assert search_routes._search_pipelines("stromvertrag", home["beate"]) == []
