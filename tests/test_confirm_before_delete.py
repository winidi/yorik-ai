"""Deletes are staged, never applied on the LLM's say-so.

Phase 2 contract:
  * a delete_* skill leaves the row in place and stages a pending
    action with rollback_kind "apply:<kind>";
  * the confirm route (pending_actions.apply) runs the delete;
  * cancel discards the card and touches nothing;
  * lookup skills only return rows the caller may see;
  * non-admin roles never see install_connector.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import seed_user

IDS: dict[str, str] = {}


def _mk_ctx(*, role: str, user_id: str):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=user_id)


@pytest.fixture
def household(fresh_app):
    """Admin + member, each with calendars; one event and one task per user."""
    from backend.calendars import ensure_calendars_for_user
    from backend.database import get_conn

    from backend import spaces as _sp
    IDS["admin"] = seed_user(name="Admin", role="admin", email="admin@example.com")
    IDS["member"] = seed_user(name="Member", role="member", email="member@example.com")
    # Same bootstrap users.create_user does: personal space first, then
    # calendars (which resolve their space_id from it).
    _sp.ensure_workspace_exists(IDS["admin"], "Admin")
    for who, name in (("admin", "Admin"), ("member", "Member")):
        _sp.ensure_personal_space(IDS[who], name)
        ensure_calendars_for_user(IDS[who], name)

    rows: dict[str, int] = {}
    with get_conn() as conn:
        for who in ("admin", "member"):
            cal = conn.execute(
                "SELECT id, space_id FROM calendars WHERE owner_user_id = ? AND kind = 'personal' LIMIT 1",
                (IDS[who],),
            ).fetchone()
            cur = conn.execute(
                "INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id, space_id) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                (f"Zahnarzt {who}", "2026-09-10T10:00:00", "2026-09-10T11:00:00",
                 cal["id"], IDS[who], cal["space_id"]),
            )
            rows[f"event_{who}"] = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO tasks (title, created_by_user_id) VALUES (?, ?)",
                (f"Milch kaufen {who}", IDS[who]),
            )
            rows[f"task_{who}"] = cur.lastrowid
        conn.commit()
    return rows


def _event_exists(event_id: int) -> bool:
    from backend.database import get_conn
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone() is not None


class TestDeferredDelete:
    def test_delete_event_only_stages(self, household):
        from backend.skills.delete_calendar_event.skill import execute
        from backend import pending_actions as pa
        eid = household["event_admin"]
        result = asyncio.run(execute(ctx=_mk_ctx(role="admin", user_id=IDS["admin"]), event_id=eid))
        assert result["pending"] is True
        assert result["_llm_hint"].startswith("shown_to_user:")
        assert _event_exists(eid), "the row must survive until the user confirms"
        row = pa.get(result["pending_id"])
        assert row and pa.is_deferred(row)
        assert row["preview"]["mode"] == "confirm_before"

    def test_confirm_applies_the_delete(self, household):
        from backend.skills.delete_calendar_event.skill import execute
        from backend import pending_actions as pa
        eid = household["event_admin"]
        result = asyncio.run(execute(ctx=_mk_ctx(role="admin", user_id=IDS["admin"]), event_id=eid))
        applied = pa.apply(result["pending_id"])
        assert applied["applied"] == "delete_event" and applied["deleted"] == 1
        assert not _event_exists(eid)

    def test_cancel_keeps_the_row(self, household):
        from backend.skills.delete_calendar_event.skill import execute
        from backend import pending_actions as pa
        eid = household["event_admin"]
        result = asyncio.run(execute(ctx=_mk_ctx(role="admin", user_id=IDS["admin"]), event_id=eid))
        out = pa.rollback(result["pending_id"])
        assert out == {"discarded": "delete_event"}
        assert _event_exists(eid)

    def test_confirm_route_runs_the_delete(self, household):
        """End to end through the HTTP route the card calls."""
        from fastapi.testclient import TestClient
        from backend import auth_sessions
        from backend.skills.delete_task.skill import execute
        tid = household["task_admin"]
        result = asyncio.run(execute(ctx=_mk_ctx(role="admin", user_id=IDS["admin"]), task_id=tid))
        # a session for the admin who staged it
        from backend.database import get_conn
        with get_conn() as conn:
            conn.execute("UPDATE user_profiles SET password_hash = ? WHERE id = ?",
                         (auth_sessions.hash_password("pw-123456789"), IDS["admin"]))
            conn.commit()
        sid = auth_sessions.create_session(IDS["admin"], user_agent="pytest", ip="127.0.0.1")
        from backend import main as backend_main
        client = TestClient(backend_main.app)
        client.cookies.set(auth_sessions.COOKIE_NAME, sid)
        r = client.post(f"/api/pending/{result['pending_id']}/confirm")
        assert r.status_code == 200, r.text
        assert r.json()["applied"]["applied"] == "delete_task"
        with get_conn() as conn:
            assert conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is None

    def test_member_cannot_stage_admins_event(self, household):
        from backend.skills.delete_calendar_event.skill import execute
        with pytest.raises(PermissionError, match="owner"):
            asyncio.run(execute(ctx=_mk_ctx(role="member", user_id=IDS["member"]),
                                event_id=household["event_admin"]))
        assert _event_exists(household["event_admin"])

    def test_second_delete_in_a_turn_is_refused(self, household):
        from backend.skills.delete_task.skill import execute
        ctx = _mk_ctx(role="admin", user_id=IDS["admin"])

        async def one_turn():
            # Both calls inside one task context — that is what one
            # /api/ask turn looks like (the throttle is a ContextVar).
            await execute(ctx=ctx, task_id=household["task_admin"])
            with pytest.raises(ValueError, match="REFUSED"):
                await execute(ctx=ctx, task_id=household["task_member"])

        asyncio.run(one_turn())


class TestScopedLookups:
    def test_find_event_by_title_is_scoped(self, household):
        from backend.skills.find_event_by_title.skill import execute
        member_hits = asyncio.run(execute(ctx=_mk_ctx(role="member", user_id=IDS["member"]), query="Zahnarzt",
                                          days_back=0, days_forward=400))
        titles = {m["title"] for m in member_hits["matches"]}
        assert "Zahnarzt member" in titles
        assert "Zahnarzt admin" not in titles

    def test_find_task_by_title_is_scoped(self, household):
        from backend.skills.find_task_by_title.skill import execute
        member_hits = asyncio.run(execute(ctx=_mk_ctx(role="member", user_id=IDS["member"]), query="Milch"))
        titles = {m["title"] for m in member_hits["matches"]}
        assert "Milch kaufen member" in titles
        assert "Milch kaufen admin" not in titles

    def test_platform_admin_sees_all_tasks(self, household):
        from backend.skills.find_task_by_title.skill import execute
        hits = asyncio.run(execute(ctx=_mk_ctx(role="platform_admin", user_id=IDS["admin"]), query="Milch"))
        assert len(hits["matches"]) == 2


class TestToolAcl:
    def test_install_connector_hidden_from_members(self):
        from backend.agent.tools import ToolRegistry, Tool

        class _T(Tool):
            def __init__(self, name):
                self._n = name

            @property
            def name(self):
                return self._n

            @property
            def description(self):
                return "x"

            @property
            def json_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, ctx, args):  # pragma: no cover
                raise NotImplementedError

        reg = ToolRegistry()
        reg.register(_T("install_connector"))
        reg.register(_T("invoke_skill"))
        assert "install_connector" not in reg.names_for_role("member")
        assert "install_connector" not in reg.names_for_role("restricted")
        assert "install_connector" in reg.names_for_role("admin")
        assert "install_connector" in reg.names_for_role("platform_admin")
        assert [t["function"]["name"] for t in reg.schemas(names=reg.names_for_role("member"))] == ["invoke_skill"]
