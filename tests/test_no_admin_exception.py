"""Admin rights are for settings, users and backups — not for seeing or
changing what belongs to someone else (audit
docs/audits/2026-09-22-berechtigungen.md, package 10)."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from tests.conftest import login_client


@pytest.fixture
def household(fresh_app):
    from backend import spaces as S
    from backend.calendars import ensure_calendars_for_user
    from backend.database import get_conn
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    kid_c, kid = login_client(fresh_app, role="restricted", name="Kid", email="k@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate"), (kid, "Kid")):
        S.ensure_personal_space(uid, name); ensure_calendars_for_user(uid, name)
    today = date.today().isoformat()
    with get_conn() as conn:
        tid = conn.execute("INSERT INTO tasks (title, done, created_by_user_id, space_id) VALUES ('Beates Aufgabe', 0, ?, ?) RETURNING id",
                           (beate, S.personal_space_id(beate))).fetchone()["id"]
        ktid = conn.execute("INSERT INTO tasks (title, done, created_by_user_id, space_id) VALUES ('Kids Aufgabe', 0, ?, ?) RETURNING id",
                            (kid, S.personal_space_id(kid))).fetchone()["id"]
        conn.execute("INSERT INTO task_assignees (task_id, user_id) VALUES (?, ?)", (ktid, kid))
        cal = conn.execute("SELECT id FROM calendars WHERE owner_user_id=? AND kind='personal'", (beate,)).fetchone()["id"]
        conn.execute("INSERT INTO events (title, starts_at, ends_at, all_day, calendar_id, owner_user_id) VALUES ('Yoga', ?, ?, 0, ?, ?)",
                     (f"{today}T18:00:00", f"{today}T19:00:00", cal, beate))
        conn.commit()
    return {"dirk": (dirk_c, dirk), "beate": (beate_c, beate), "kid": (kid_c, kid), "task": tid, "kid_task": ktid}


def test_the_operator_changes_nothing_that_is_not_theirs(household):
    dirk_c, _ = household["dirk"]
    assert dirk_c.patch(f"/api/tasks/{household['task']}", json={"done": True}).status_code == 403
    assert dirk_c.delete(f"/api/tasks/{household['task']}").status_code == 403


def test_the_chat_gate_follows_the_same_rule(household):
    from backend import calendars as C
    from backend.database import get_conn
    _, dirk = household["dirk"]; _, beate = household["beate"]
    with get_conn() as conn:
        task = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (household["task"],)).fetchone())
        kid_task = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (household["kid_task"],)).fetchone())
    with pytest.raises(C.RowOwnerPermissionError):
        C.require_row_owner_or_admin("admin", dirk, task, subject="task", owner_col="created_by_user_id")
    with pytest.raises(C.RowOwnerPermissionError):
        C.require_row_owner_or_admin("platform_admin", dirk, task, subject="task", owner_col="created_by_user_id")
    C.require_row_owner_or_admin("member", beate, task, subject="task", owner_col="created_by_user_id")      # the owner
    # (a parent on a child's task goes through the same can_write_row — covered in test_task_assignment)


def test_calendar_reads_have_no_operator_exception(household):
    from backend import calendars as C
    from backend.skills.check_calendar.skill import execute
    from backend.skills.registry import Registry, SkillContext
    _, dirk = household["dirk"]; _, beate = household["beate"]
    out = asyncio.run(execute(SkillContext(Registry(), role="platform_admin", user_id=dirk)))
    assert not [e for e in (out.get("events") or []) if e.get("title") == "Yoga"]
    fb = C.freebusy([beate], f"{date.today()}T00:00:00", f"{date.today()}T23:59:59",
                    requested_by_user_id=dirk, requested_by_role="platform_admin")
    assert fb[beate] == []                                                          # nothing shared → nothing


def test_nobody_is_provisioned_as_superuser_or_immich_admin(fresh_app, monkeypatch):
    from backend import external_users as EU
    posted = []
    class _R:
        ok, status_code = True, 201
        def json(self): return {"id": 42, "results": [], "accessToken": "t", "userId": "u"}
        text = ""
    monkeypatch.setattr(EU.requests, "get", lambda url, **kw: type("G", (), {"ok": True, "status_code": 200, "json": lambda self: {"results": []}, "text": ""})())
    monkeypatch.setattr(EU.requests, "post", lambda url, **kw: posted.append((url, kw.get("json"))) or _R())
    monkeypatch.setattr(EU, "_paperless_admin_settings", lambda: {"base_url": "http://p", "api_key": "admin"}, raising=False)
    try:
        EU.provision_paperless("u1", "Dirk", "d@example.local", "pw", is_admin=True, _store=False)
    except Exception:
        pass
    users = [j for u, j in posted if u.endswith("/api/users/") and j]
    assert users and users[0]["is_superuser"] is False and users[0]["is_staff"] is False
    assert EU.paperless_username_for("dirk+home@example.local", "Dirk") == "dirk_home"


def test_the_proxy_browses_as_the_persons_own_account():
    from backend.paperless_proxy import _paperless_username
    assert _paperless_username({"role": "platform_admin", "email": "dirk@winiecki.ai", "name": "Dirk"}) == "dirk"
