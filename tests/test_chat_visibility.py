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


# ── a household: Dirk (operator), Beate, a child ─────────────────────

import pytest


@pytest.fixture
def house(fresh_app):
    from backend import spaces as S
    h = {
        "dirk":  seed_user(name="Dirk Winiecki", role="platform_admin", first_name="Dirk", email="dirk@example.local"),
        "beate": seed_user(name="Beate Winiecki", role="member", first_name="Beate", email="beate@example.local"),
        "kid":   seed_user(name="Clara Winiecki", role="restricted", first_name="Clara", email="clara@example.local"),
    }
    S.ensure_workspace_exists(h["dirk"], "Dirk")
    for k in ("dirk", "beate", "kid"):
        S.ensure_personal_space(h[k], k)
        h[f"{k}_space"] = S.personal_space_id(h[k])
    return h


def ctx_for(h, who, role=None):
    from backend.skills.registry import Registry, SkillContext
    roles = {"dirk": "platform_admin", "beate": "member", "kid": "restricted"}
    return SkillContext(Registry(), role=role or roles[who], user_id=h[who])


def run(coro):
    return asyncio.run(coro)


def _contact(h, owner, name):
    from backend.database import get_conn
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO contacts (display_name, kind, created_by_user_id, space_id) VALUES (?, 'person', ?, ?)",
            (name, h[owner], h[f"{owner}_space"]))
        conn.commit()
        return cur.lastrowid


# ── L2: personal connectors run on the person's own account ──────────

def test_immich_connector_uses_the_askers_key(house, monkeypatch):
    from backend import connectors, external_users
    seen = {}
    spec = connectors.get("immich")
    monkeypatch.setattr(spec, "invoke", lambda **p: seen.update(p) or {"ok": True})
    monkeypatch.setattr(external_users, "get_user_immich_creds",
                        lambda uid: {"api_key": f"key-{uid}"} if uid == house["dirk"] else None)
    assert run(connectors.invoke("immich", {"op": "recent"}, user_id=house["dirk"]))["ok"]
    assert seen["creds_override"] == {"api_key": f"key-{house['dirk']}"}
    seen.clear()
    got = run(connectors.invoke("immich", {"op": "recent"}, user_id=house["kid"]))
    assert not got["ok"] and not seen            # no account → nothing, never the admin library


@pytest.mark.parametrize("name", ["email-imap", "banking-fints"])
def test_household_account_connectors_refuse_a_person(house, name):
    from backend import connectors
    if not connectors.get(name):
        pytest.skip(f"{name} not registered here")
    got = run(connectors.invoke(name, {"op": "list_recent"}, user_id=house["beate"]))
    assert not got["ok"] and "household" in got["error"]


# ── L1: an upload is read by its uploader only ───────────────────────

def test_upload_read_by_uploader_only(house):
    from backend.documents import get_docs_conn, DOCS_DB_PATH
    from backend.skills.read_document.skill import execute as read_document
    with get_docs_conn(DOCS_DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO documents (title, path, mime_type, bytes, tags, allowed_roles, owner_user_id) "
            "VALUES ('Beates Arztbrief', '', 'text/plain', 10, '', 'admin,member,restricted', ?)", (house["beate"],))
        doc_id = cur.lastrowid
        conn.execute("INSERT INTO document_chunks (doc_id, chunk_index, text) VALUES (?, 0, 'Befund: vertraulich')", (doc_id,))
        conn.commit()
    assert "vertraulich" in run(read_document(ctx_for(house, "beate"), doc_id=doc_id))["text"]
    for who in ("dirk", "kid"):
        got = run(read_document(ctx_for(house, who), doc_id=doc_id))
        assert got["ok"] is False and "vertraulich" not in str(got)


# ── L5: skills that take a contact id only take a visible one ────────

def test_letter_recipient_must_be_visible(house):
    from backend.skills.compose_check_recipient.skill import execute as check_recipient
    cid = _contact(house, "beate", "Beates Frauenärztin")
    mine = run(check_recipient(ctx_for(house, "beate"), contact_id=cid, template_id="brief"))
    assert "Frauenärztin" in str(mine)
    theirs = run(check_recipient(ctx_for(house, "dirk"), contact_id=cid, template_id="brief"))
    assert "Frauenärztin" not in str(theirs)


def test_known_provider_sees_only_visible_contacts(house):
    from backend.skills.find_known_provider.skill import execute as find_known_provider
    _contact(house, "beate", "Zahnarzt Dr. Beate-Privat")
    assert "Beate-Privat" in str(run(find_known_provider(ctx_for(house, "beate"), category="zahnarzt")))
    assert "Beate-Privat" not in str(run(find_known_provider(ctx_for(house, "dirk"), category="zahnarzt")))
