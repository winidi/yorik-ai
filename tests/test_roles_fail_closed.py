"""A missing role is the least role, never admin (2026-09-29): a profile
without a role used to pass every admin check that ran through
normalize_role, and /api/skills took the role from the query string."""

from backend.auth import normalize_role
from tests.conftest import login_client


def test_no_role_is_the_least_role():
    assert normalize_role(None) == "restricted"
    assert normalize_role("") == "restricted"
    assert normalize_role("Admin") == "admin"


def test_skill_list_follows_the_session_not_the_query(fresh_app):
    kid, _ = login_client(fresh_app, role="restricted", name="Clara", email="clara@example.local")
    boss, _ = login_client(fresh_app, role="platform_admin", name="Dirk", email="dirk@example.local")
    mine = {s["name"] for s in kid.get("/api/skills").json()}
    forged = {s["name"] for s in kid.get("/api/skills?role=admin").json()}
    admins = {s["name"] for s in boss.get("/api/skills").json()}
    assert forged == mine and mine < admins
