"""An opt-in app that is off leaves no trace: no dock entry, no skill
in chat, none over MCP; switching it on brings everything back."""

import asyncio

import pytest

from tests.conftest import login_client, seed_user


def test_recordings_off_hides_its_skills_everywhere(fresh_app):
    from backend import apps as A
    from backend.skills import get_registry
    from backend.mcp_server import list_tools
    client, uid = login_client(fresh_app, role="admin", name="Dirk")
    user = {"id": uid, "role": "admin", "agent_may_confirm_deletes": False}

    A.set_opt_in_enabled("recordings", False)
    assert "recordings" not in {a["id"] for a in client.get("/api/apps").json()}
    assert "start_recording" not in {r["name"] for r in get_registry().index(role="admin")}
    assert "start_recording" not in {t["name"] for t in list_tools(user)}

    A.set_opt_in_enabled("recordings", True)
    assert "recordings" in {a["id"] for a in client.get("/api/apps").json()}
    assert {"start_recording", "finish_recording", "recording_status", "recording_report"} <= {r["name"] for r in get_registry().index(role="admin")}
    assert "start_recording" in {t["name"] for t in list_tools(user)}


def test_disabled_app_skill_refuses_to_run(fresh_app):
    from backend import apps as A
    from backend.skills import get_registry
    from backend.skills.registry import SkillContext
    uid = seed_user(name="Dirk", role="admin")
    A.set_opt_in_enabled("recordings", False)
    ctx = SkillContext(get_registry(), role="admin", user_id=uid, conversation_id="c1")
    with pytest.raises(Exception):
        asyncio.run(get_registry().invoke("recording_status", ctx=ctx))
    A.set_opt_in_enabled("recordings", True)


def test_pipelines_is_opt_in_but_stays_on_where_it_is_used(fresh_app):
    """Pipelines became opt-in and experimental on 2026-09-29. A fresh
    install starts without it; an install that already has pipelines
    keeps it, and the switch works normally afterwards."""
    from backend import apps as A
    from backend.database import get_conn
    client, uid = login_client(fresh_app, role="admin", name="Dirk")
    ids = lambda: {a["id"] for a in client.get("/api/apps").json()}
    assert "pipelines" not in ids()                              # fresh: off, nothing written
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM app_settings WHERE key = 'app_enabled_pipelines'").fetchone() is None
        conn.execute("INSERT INTO pipelines (owner_user_id, kind, title) VALUES (?, 'nachfassen', 'Kündigung')", (uid,))
        conn.commit()
    assert "pipelines" in ids()                                  # in use: kept on, and recorded
    with get_conn() as conn:
        assert conn.execute("SELECT value FROM app_settings WHERE key = 'app_enabled_pipelines'").fetchone()[0] == "1"
    A.set_opt_in_enabled("pipelines", False)
    assert "pipelines" not in ids()                              # the admin's "off" wins over the rows
    opt = {a["id"]: a for a in client.get("/api/apps/opt-in").json()}
    assert all(opt[i]["experimental"] for i in ("pipelines", "finance", "write", "recordings"))
    assert not opt["whatsapp"]["experimental"]


def test_compose_is_retired_and_a_skill_toggle_keeps_app_skills_out_of_the_saved_list(fresh_app):
    """Compose was retired on 2026-09-29: no app entry, no chat skill.
    Toggling one skill in Settings writes back only the admin's choices,
    so an app's skills come back when the app is switched on again."""
    from backend import apps as A
    from backend.skills import get_registry
    from backend.skills.registry import get_admin_disabled_skills
    client, _ = login_client(fresh_app, role="admin", name="Dirk")
    assert "compose" not in {a["id"] for a in client.get("/api/apps").json()}
    offered = {r["name"] for r in get_registry().index(role="admin")}
    assert not {"compose_draft", "list_compose_templates", "pick_compose_template"} & offered

    A.set_opt_in_enabled("recordings", False)
    assert client.patch("/api/skills/check_tasks", json={"enabled": False}).status_code == 200
    from backend.skills.registry import NICHE_OFF_BY_DEFAULT
    # the niche skills that are off by default stay off; not the
    # recording skills, not Compose
    assert get_admin_disabled_skills() == NICHE_OFF_BY_DEFAULT | {"check_tasks"}
    A.set_opt_in_enabled("recordings", True)
    assert "start_recording" in {r["name"] for r in get_registry().index(role="admin")}
    client.patch("/api/skills/check_tasks", json={"enabled": True})
