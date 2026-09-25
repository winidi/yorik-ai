"""Who sees what through the chat (audit 2026-09-25,
docs/audits/2026-09-25-chat-sichtbarkeit.md): the chat knows who asks,
"mine" means the person's own list, and a skill never hands out what
the app would refuse."""

import asyncio

from tests.conftest import seed_user


def _prompt(role, user_id, identified_name=None):
    from backend import ask
    _, prompt = asyncio.run(ask._build_user_and_prompt(
        role=role, user_language="de", identified_name=identified_name, user_id=user_id))
    return prompt


# ── A1: the typed chat names the person who asks ─────────────────────

def test_typed_chat_knows_who_asks(fresh_app):
    uid = seed_user(name="Beate Winiecki", role="member", first_name="Beate")
    p = _prompt("member", uid)
    assert "The logged-in user is **Beate Winiecki**" in p
    assert "no logged-in user" not in p
    assert "don't greet by name" in p          # typed, not voice


def test_voice_greets_the_voice_name(fresh_app):
    uid = seed_user(name="Dirk Winiecki", role="admin", first_name="Dirk")
    p = _prompt("admin", uid, identified_name="Dirk")
    assert "identified as **Dirk** (matched via voice)" in p
    assert "The logged-in user is **Dirk Winiecki**" in p


def test_no_person_no_identity(fresh_app):
    assert "no logged-in user" in _prompt("member", None)


def test_skill_menu_follows_the_role(fresh_app):
    from backend.skills.registry import get_registry
    reg = get_registry()
    grown_up = {r["name"] for r in reg.index()
                if "restricted" not in (r.get("permissions") or []) and "*" not in (r.get("permissions") or [])}
    assert grown_up, "fixture assumption: some skills are not for restricted accounts"
    assert not (grown_up & {r["name"] for r in reg.index(role="restricted")})
    assert grown_up <= {r["name"] for r in reg.index(role="platform_admin")}   # inherits admin, as in invoke()
