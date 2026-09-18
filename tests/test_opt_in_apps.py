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
