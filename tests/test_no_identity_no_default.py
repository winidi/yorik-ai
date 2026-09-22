"""A missing identity is an error, never "user 1" or "admin" (audit
docs/audits/2026-09-22-berechtigungen.md, package 11: 3.8, 3.11, 4.7,
4.8, 4.10)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.conftest import login_client


def test_skills_refuse_to_guess_the_person(fresh_app):
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.email_briefing.skill import execute as briefing
    from backend.skills.universal_search.skill import execute as search
    from backend.skills.whatsapp_draft.skill import execute as draft
    from backend.skills.check_tasks.skill import execute as tasks
    from backend.database import get_conn
    nobody = SkillContext(Registry(), role="member", user_id=None)
    for skill, kwargs in ((briefing, {}), (search, {"query": "x"}), (draft, {"chat_jid": "4917@s.whatsapp.net"})):
        with pytest.raises(ValueError, match="signed-in user"):
            asyncio.run(skill(nobody, **kwargs))
    with get_conn() as conn:
        conn.execute("INSERT INTO tasks (title, done) VALUES ('Für alle sichtbar?', 0)"); conn.commit()
    out = asyncio.run(tasks(nobody))
    assert not [t for t in (out.get("tasks") or out.get("matches") or []) if "sichtbar" in t.get("title", "")]


def test_routes_need_a_signed_in_person(fresh_app):
    anon = TestClient(fresh_app)
    assert anon.post("/api/skills/check_tasks/invoke", json={}).status_code in (401, 403)
    assert anon.post("/api/whatsapp/draft", json={"chat_jid": "4917@s.whatsapp.net"}).status_code in (401, 403)


def test_background_work_uses_the_persons_real_role(fresh_app):
    from backend import spaces as S
    _, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    assert S.role_of(beate) == "member"
    assert S.role_of(None) == "" and S.role_of("00000000-0000-0000-0000-000000000000") == ""


def test_a_pending_action_needs_its_person(fresh_app):
    from backend import pending_actions as PA
    with pytest.raises(ValueError, match="signed-in user"):
        PA._require_user(type("Ctx", (), {"user_id": None})())
    assert PA._require_user(type("Ctx", (), {"user_id": "u-1"})()) == "u-1"
