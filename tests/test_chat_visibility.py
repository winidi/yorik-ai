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


# ── A2–A6: "meine" means the person's own ────────────────────────────

def _household_space():
    from backend.database import get_conn
    with get_conn() as conn:
        return int(conn.execute("SELECT id FROM spaces WHERE slug = 'household'").fetchone()["id"])


def _task(title, *, creator=None, assignees=(), space=None, due="2026-09-01"):
    from backend.database import get_conn
    with get_conn() as conn:
        tid = conn.execute(
            "INSERT INTO tasks (title, due_date, done, created_by_user_id, space_id) VALUES (?, ?, 0, ?, ?)",
            (title, due, creator, space)).lastrowid
        for u in assignees:
            conn.execute("INSERT INTO task_assignees (task_id, user_id) VALUES (?, ?)", (tid, u))
        conn.commit()
    return tid


@pytest.fixture
def tasks(house):
    from backend.database import get_conn
    hh = _household_space()
    for k in ("beate", "kid"):
        with get_conn() as conn:
            conn.execute("INSERT INTO space_members (space_id, user_id, level) VALUES (?, ?, 'write') "
                         "ON CONFLICT DO NOTHING", (hh, house[k]))
            conn.commit()
    d, b, k = house["dirk"], house["beate"], house["kid"]
    return {
        "own":      _task("Steuer machen", creator=d, assignees=[d], space=hh),
        "for_kid":  _task("Zimmer aufräumen", creator=d, assignees=[k], space=hh),
        "beates":   _task("Arzttermin ausmachen", creator=b, assignees=[b], space=hh),
        "together": _task("Urlaub buchen", creator=b, assignees=[b, d], space=hh),
        "chore":    _task("Müll rausbringen", creator=None, space=hh),
    }


def _titles_and_people(house, who, **kw):
    from backend.skills.check_tasks.skill import execute as check_tasks
    got = run(check_tasks(ctx_for(house, who), include_undated=True, include_rows=True, **kw))
    return {t["title"]: t["person"] for t in got["tasks"]}, got["scope"]


def test_my_tasks_are_mine(house, tasks):
    got, scope = _titles_and_people(house, "dirk")
    assert scope == "mine"
    assert set(got) == {"Steuer machen", "Urlaub buchen", "Müll rausbringen"}
    assert got["Steuer machen"] is None and got["Urlaub buchen"] == "+ Beate"


def test_everyone_names_whose(house, tasks):
    got, scope = _titles_and_people(house, "dirk", everyone=True)
    assert scope == "everyone"
    assert got["Zimmer aufräumen"] == "Clara" and got["Arzttermin ausmachen"] == "Beate"


def test_someone_elses_list_by_name(house, tasks):
    got, _ = _titles_and_people(house, "dirk", person="Clara")
    assert set(got) == {"Zimmer aufräumen"}


def test_day_plan_holds_only_own_tasks(house, tasks):
    from backend import day_plans
    ctx = day_plans.context_for(house["dirk"], "2026-09-25", role="platform_admin")
    titles = {t["title"] for t in ctx["open_tasks"]}
    assert "Zimmer aufräumen" not in titles and "Arzttermin ausmachen" not in titles
    assert "Steuer machen" in titles


def test_today_counts_own_overdue(house, tasks, fresh_app):
    from fastapi.testclient import TestClient
    from backend import auth_sessions
    c = TestClient(fresh_app)
    c.cookies.set(auth_sessions.COOKIE_NAME, auth_sessions.create_session(house["dirk"], user_agent="t", ip="127.0.0.1"))
    got = c.get("/api/today").json()
    assert got["tasks_overdue_count"] == 3          # own, together, chore


# ── calendar: own + household + invitations; others' private = Busy ──

@pytest.fixture
def events(house):
    from backend import calendars as C
    from backend.database import get_conn
    fam = C.create_calendar(name="Familie", owner_user_id=house["dirk"], kind="shared")
    dirk_cal = C.create_calendar(name="Dirk", owner_user_id=house["dirk"])
    beate_cal = C.create_calendar(name="Beate", owner_user_id=house["beate"])
    with get_conn() as conn:    # Beate lets Dirk see her personal space
        conn.execute("INSERT INTO space_members (space_id, user_id, level) VALUES (?, ?, 'read')",
                     (house["beate_space"], house["dirk"]))
        conn.execute("INSERT INTO space_members (space_id, user_id, level) VALUES (?, ?, 'write') "
                     "ON CONFLICT DO NOTHING", (_household_space(), house["beate"]))
        def ev(title, cal, owner, vis="default"):
            conn.execute("INSERT INTO events (title, starts_at, ends_at, calendar_id, owner_user_id, visibility, location) "
                         "VALUES (?, '2026-10-02T10:00:00', '2026-10-02T11:00:00', ?, ?, ?, 'Praxis Mitte')",
                         (title, cal, owner, vis))
        ev("Elternabend", fam, house["beate"])
        ev("Zahnarzt Dirk", dirk_cal, house["dirk"])
        ev("Yoga Beate", beate_cal, house["beate"])
        ev("Frauenarzt", fam, house["beate"], vis="private")
        conn.commit()


def _cal(house, who, **kw):
    from backend.skills.check_calendar.skill import execute as check_calendar
    return run(check_calendar(ctx_for(house, who), start_iso="2026-10-02", end_iso="2026-10-02T23:59:59", **kw))


def test_my_calendar_is_mine_and_the_households(house, events):
    got = {e["title"]: e["who"] for e in _cal(house, "dirk")["events"]}
    assert "Yoga Beate" not in got                           # her personal calendar, though shared
    assert got["Zahnarzt Dirk"] == "" and got["Elternabend"] == "Beate"
    assert "Frauenarzt" not in got and got["Busy"] == "Beate"   # private → Busy, as in the app


def test_everyone_calendar_names_whose(house, events):
    got = {e["title"]: e["who"] for e in _cal(house, "dirk", everyone=True)["events"]}
    assert got["Yoga Beate"] == "Beate"


def test_private_title_cannot_be_probed(house, events):
    assert _cal(house, "dirk", title_contains="Frauenarzt", everyone=True)["events"] == []
    assert [e["title"] for e in _cal(house, "beate", title_contains="Frauenarzt")["events"]] == ["Frauenarzt"]


# ── L6: address suggestions are the searcher's ───────────────────────

def test_address_suggestions_stay_with_the_searcher(house, monkeypatch):
    from backend import contact_address_scraper as S
    cid = _contact(house, "dirk", "Handwerker Meier")
    monkeypatch.setattr(S, "gather_passages", lambda c, owner_user_id=None:
                        [{"source_kind": "whatsapp", "source_ref": "m1", "text": "Musterweg 1"}])
    monkeypatch.setattr(S, "call_llm_extract", lambda p:
                        [{"line1": "Musterweg 1", "city": "Köln", "source_index": 0,
                          "excerpt": "aus Beates Chat"}])
    got = S.scrape_and_cache(cid, owner_user_id=house["beate"], use_cache=False)
    assert got["candidates"][0]["excerpt"] == "aus Beates Chat"
    monkeypatch.setattr(S, "call_llm_extract", lambda p: [])
    dirk = S.scrape_and_cache(cid, owner_user_id=house["dirk"], use_cache=True)
    assert "aus Beates Chat" not in str(dirk)
    assert S.scrape_and_cache(cid, owner_user_id=None)["candidates"] == []


# ── L9: local document lists hold the person's own uploads ───────────

def test_local_documents_are_the_uploaders(house):
    from backend import documents as D
    with D.get_docs_conn(D.DOCS_DB_PATH) as conn:
        conn.execute("INSERT INTO documents (title, path, allowed_roles, owner_user_id) VALUES ('Beates Vertrag', '', 'admin,member', ?)", (house["beate"],))
        conn.commit()
    titles = lambda who: {d["title"] for d in D.list_documents(role="admin", owner_user_id=house[who] if who else "")}
    assert titles("beate") == {"Beates Vertrag"}
    assert titles("dirk") == set() and titles(None) == set()


# ── L10: no admin look into others' drafts and web lookups ───────────

def test_admin_sees_own_drafts_and_web_log_only(house, fresh_app):
    from fastapi.testclient import TestClient
    from backend import auth_sessions
    from backend.database import get_conn
    with get_conn() as conn:
        did = conn.execute("INSERT INTO compose_drafts (user_id, subject) VALUES (?, 'Kündigung Beate')", (house["beate"],)).lastrowid
        conn.execute("INSERT INTO web_visits (user_id, action, query) VALUES (?, 'web_lookup', 'Beates Suche')", (house["beate"],))
        conn.commit()
    c = TestClient(fresh_app)
    c.cookies.set(auth_sessions.COOKIE_NAME, auth_sessions.create_session(house["dirk"], user_agent="t", ip="127.0.0.1"))
    assert c.get("/api/compose/saved-drafts").json() == []
    assert c.get("/api/web/visits").json() == []
    assert c.get(f"/api/compose/saved-draft/{did}").status_code in (403, 404)
    from backend.skills.delete_compose_draft.skill import execute as delete_draft
    with pytest.raises(ValueError):
        run(delete_draft(ctx_for(house, "dirk"), draft_id=did))


# ── W1–W5: the chat writes only where the app would ──────────────────

def test_no_event_into_someone_elses_calendar(house):
    from backend import calendars as C
    from backend.skills.add_calendar_event.skill import execute as add_event
    beate_cal = C.create_calendar(name="Beate", owner_user_id=house["beate"])
    with pytest.raises(PermissionError):
        run(add_event(ctx_for(house, "dirk"), title="Überraschung", starts_at="2027-01-10T10:00:00",
                      calendar_id=beate_cal))


def test_mirror_events_stay_read_only(house):
    from backend import calendars as C
    from backend.database import get_conn
    from backend.skills.update_calendar_event.skill import execute as update_event
    from backend.skills.delete_calendar_event.skill import execute as delete_event
    mirror = C.create_calendar(name="Google", owner_user_id=house["dirk"])
    with get_conn() as conn:
        conn.execute("UPDATE calendars SET read_only = 1 WHERE id = ?", (mirror,))
        eid = conn.execute("INSERT INTO events (title, starts_at, calendar_id, owner_user_id) "
                           "VALUES ('Gespiegelt', '2027-01-10T10:00:00', ?, ?)", (mirror, house["dirk"])).lastrowid
        conn.commit()
    with pytest.raises(PermissionError):
        run(update_event(ctx_for(house, "dirk"), event_id=eid, title="neu"))
    with pytest.raises(PermissionError):
        run(delete_event(ctx_for(house, "dirk"), event_id=eid))


def test_day_plan_leaves_others_tasks_alone(house):
    from backend import day_plans
    from backend.database import get_conn
    private = _task("Geschenk für Dirk", creator=house["beate"], assignees=[house["beate"]], space=house["beate_space"])
    out = day_plans.apply_plan(user_id=house["dirk"], plan_date="2026-09-26", role="platform_admin",
                               items=[{"title": "Umbenannt", "task_id": private}])
    assert out["summary"].get("skipped") == 1
    with get_conn() as conn:
        assert conn.execute("SELECT title FROM tasks WHERE id = ?", (private,)).fetchone()["title"] == "Geschenk für Dirk"


def test_ledger_only_into_own_conversation(house):
    from backend.agent import conversation_io as io
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("INSERT INTO agent_conversations (id, user_id, user_role, messages_json, ledger_json) "
                     "VALUES ('conv-beate', ?, 'member', '[]', '{}')", (house["beate"],))
        conn.commit()
    io.save_ledger("conv-beate", {"planted": True}, house["dirk"])
    assert io.load_ledger("conv-beate", house["beate"]) == {}
    io.save_ledger("conv-beate", {"mine": True}, house["beate"])
    assert io.load_ledger("conv-beate", house["beate"]) == {"mine": True}


# ── package 6: the rest ──────────────────────────────────────────────

def test_event_title_search_hides_private_and_needs_a_person(house):
    from datetime import datetime, timedelta
    from backend import calendars as C
    from backend.database import get_conn
    from backend.skills.find_event_by_title.skill import execute as find_event
    fam = C.create_calendar(name="Familie", owner_user_id=house["dirk"], kind="shared")
    when = (datetime.now() + timedelta(days=3)).replace(microsecond=0).isoformat()
    with get_conn() as conn:
        conn.execute("INSERT INTO space_members (space_id, user_id, level) VALUES (?, ?, 'write') ON CONFLICT DO NOTHING",
                     (_household_space(), house["beate"]))
        conn.execute("INSERT INTO events (title, starts_at, calendar_id, owner_user_id, visibility) "
                     "VALUES ('Frauenarzt', ?, ?, ?, 'private')", (when, fam, house["beate"]))
        conn.commit()
    assert run(find_event(ctx_for(house, "beate"), query="Frauenarzt"))["count"] == 1
    assert run(find_event(ctx_for(house, "dirk"), query="Frauenarzt"))["count"] == 0
    from backend.skills.registry import Registry, SkillContext
    assert run(find_event(SkillContext(Registry(), role="admin", user_id=None), query="Frauenarzt"))["count"] == 0


def test_task_title_search_finds_what_is_assigned_to_me(house):
    from backend.skills.find_task_by_title.skill import execute as find_task
    tid = _task("Beates Liste: Dirk holt Brot", creator=house["beate"], assignees=[house["dirk"]],
                space=house["beate_space"])
    got = run(find_task(ctx_for(house, "dirk"), query="Brot"))
    assert [m["id"] for m in got["matches"]] == [tid]
    assert run(find_task(ctx_for(house, "kid"), query="Brot"))["count"] == 0


def test_add_contact_does_not_merge_into_an_invisible_one(house):
    from backend import contacts as C
    from backend.skills.add_contact.skill import execute as add_contact
    cid = _contact(house, "beate", "Beates Freundin")
    C.add_channel(cid, kind="email", value="freundin@example.local")
    with pytest.raises(ValueError) as e:
        run(add_contact(ctx_for(house, "dirk"), display_name="Jemand", emails=["freundin@example.local"]))
    assert "Beates Freundin" not in str(e.value)


def test_saved_queries_route_is_gone(fresh_app):
    from fastapi.testclient import TestClient
    assert TestClient(fresh_app).get("/api/saved-queries").status_code in (401, 404)


def test_find_user_needs_a_person(house):
    from backend.skills.find_user.skill import execute as find_user
    from backend.skills.registry import Registry, SkillContext
    assert run(find_user(SkillContext(Registry(), role="platform_admin", user_id=None), query="Beate")).get("users") in ([], None)
    from backend.database import get_conn
    with get_conn() as conn:                  # Beate is in the household
        conn.execute("INSERT INTO space_members (space_id, user_id, level) VALUES (?, ?, 'write') ON CONFLICT DO NOTHING",
                     (_household_space(), house["beate"]))
        conn.commit()
    assert run(find_user(ctx_for(house, "dirk"), query="Beate"))["users"]
