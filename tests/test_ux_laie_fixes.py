"""Fixes from the 2026-09-29 layperson walk-through of the live app."""
import json


def test_discarded_yorik_draft_is_gone(fresh_app):
    """Composer 'discard' on a draft Yorik staged deletes the server copy,
    so the chat card cannot bring it back."""
    from tests.conftest import login_client
    from backend.database import get_conn
    client, uid = login_client(fresh_app, role="admin", name="Max", email="m@example.com")
    with get_conn() as conn:
        conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (f"pending_email_draft_{uid}",
                     json.dumps({"to": "x@web.de", "subject": "Kaffee", "body": "Hallo"})))
        conn.commit()
    assert client.get("/api/email/pending-draft").json()["draft"]["subject"] == "Kaffee"
    # looking at it keeps it
    assert client.get("/api/email/pending-draft").json()["draft"] is not None
    r = client.delete("/api/email/pending-draft")
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert client.get("/api/email/pending-draft").json()["draft"] is None


def _conversation(uid, cards):
    from backend.agent import conversation_io as cio
    msgs = [{"role": "user", "content": "schreib beate"},
            {"role": "assistant", "content": "Bereit.", "metadata": {"ui_actions": cards}}]
    cio.save_messages("conv-ux-1", "admin", uid, msgs)
    return msgs


def test_a_sent_card_stays_sent_after_reload(fresh_app):
    """A WhatsApp draft sent from the chat offered 'Send' again after a
    reload. The mark lives on the stored message now."""
    from tests.conftest import login_client
    client, uid = login_client(fresh_app, role="admin", name="Dirk", email="d@example.com")
    _conversation(uid, [{"type": "whatsapp_draft_created", "uid": "abc123", "text": "Hallo"}])
    r = client.post("/api/conversations/conv-ux-1/cards/abc123", json={"state": "sent", "data": {"text": "Hallo!"}})
    assert r.status_code == 200, r.text
    msgs = client.get("/api/conversations/conv-ux-1").json()["messages"]
    card = msgs[-1]["ui_actions"][0]
    assert card["done"] == "sent" and card["done_data"] == {"text": "Hallo!"}
    # unknown card, unknown state, someone else's chat → 404
    assert client.post("/api/conversations/conv-ux-1/cards/nope", json={"state": "sent"}).status_code == 404
    assert client.post("/api/conversations/conv-ux-1/cards/abc123", json={"state": "exploded"}).status_code == 404
    other, _ = login_client(fresh_app, role="member", name="Beate", email="b@example.com")
    assert other.post("/api/conversations/conv-ux-1/cards/abc123", json={"state": "sent"}).status_code == 404


def test_a_running_turn_does_not_wipe_a_mark(fresh_app):
    """The loop saves the messages it loaded before the click; the mark
    made in between survives that save."""
    from tests.conftest import seed_user
    from backend.agent import conversation_io as cio
    uid = seed_user(name="Dirk", role="admin", email="d2@example.com")
    msgs = _conversation(uid, [{"type": "pending_confirmation", "uid": "del1", "pending_id": "p"}])
    assert cio.mark_card("conv-ux-1", uid, "del1", "done")
    msgs = msgs + [{"role": "user", "content": "und jetzt?"}, {"role": "assistant", "content": "Gern."}]
    cio.save_messages("conv-ux-1", "admin", uid, msgs)
    stored = cio.load_messages("conv-ux-1", uid)
    card = [m for m in stored if m.get("metadata")][0]["metadata"]["ui_actions"][0]
    assert card["done"] == "done"


def test_ui_actions_get_a_card_id():
    from backend.ui_tools import _append, get_ui_actions, reset_ui_actions
    reset_ui_actions()
    a = {"type": "email_ready"}
    _append(a)
    assert a["uid"] and get_ui_actions()[0]["uid"] == a["uid"]


def _accounts(uid, *mails, default=None):
    from backend.database import get_conn
    with get_conn() as conn:
        for m in mails:
            conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                         "smtp_username, credential_key, is_default) VALUES (?, ?, 'i', 'u', 's', 'u', 'k', ?)",
                         (uid, m, 1 if m == default else 0))
        conn.commit()
        return {r["email"]: r["id"] for r in conn.execute(
            "SELECT id, email FROM email_accounts WHERE owner_user_id = ?", (uid,)).fetchall()}


def _prepare(uid, **kw):
    import asyncio
    from backend.skills.prepare_email.skill import execute
    from backend.skills.registry import Registry, SkillContext
    from backend.ui_tools import get_ui_actions, reset_ui_actions
    from backend.email_addresses import current_user_text

    async def run():
        reset_ui_actions()
        current_user_text.set("schreib an illtys@gmx.de")
        ctx = SkillContext(Registry(), role="admin", user_id=uid, conversation_id="conv-mail")
        out = await execute(ctx=ctx, to="illtys@gmx.de", subject="Kaffee", body="Hallo", **kw)
        return out, get_ui_actions()
    return asyncio.run(run())


def test_several_accounts_without_default_ask_for_the_sender(fresh_app):
    from tests.conftest import seed_user
    from backend.database import get_conn
    uid = seed_user(name="Dirk", role="admin", email="d3@example.com")
    ids = _accounts(uid, "a@gmail.com", "b@web.de")
    out, actions = _prepare(uid)
    assert out["ok"], out
    card = [a for a in actions if a["type"] == "email_ready"][0]
    assert card["account_id"] is None and {a["id"] for a in card["accounts"]} == set(ids.values())
    with get_conn() as conn:
        draft = json.loads(conn.execute("SELECT value FROM app_settings WHERE key = ?",
                                        (f"pending_email_draft_{uid}",)).fetchone()["value"])
    assert draft["account_id"] is None and draft["card"] == {"conversation_id": "conv-mail", "uid": card["uid"]}


def test_a_default_account_is_taken_and_can_be_chosen(fresh_app):
    from tests.conftest import login_client
    from backend.database import get_conn
    client, uid = login_client(fresh_app, role="admin", name="Dirk", email="d4@example.com")
    ids = _accounts(uid, "a@gmail.com", "b@web.de", default="b@web.de")
    _out, actions = _prepare(uid)
    card = [a for a in actions if a["type"] == "email_ready"][0]
    assert card["account_id"] == ids["b@web.de"]
    r = client.post("/api/email/pending-draft/account", json={"account_id": ids["a@gmail.com"], "make_default": True})
    assert r.status_code == 200, r.text
    assert client.get("/api/email/pending-draft").json()["draft"]["account_id"] == ids["a@gmail.com"]
    with get_conn() as conn:
        defaults = [r["email"] for r in conn.execute(
            "SELECT email FROM email_accounts WHERE owner_user_id = ? AND is_default = 1", (uid,)).fetchall()]
    assert defaults == ["a@gmail.com"]


def test_one_account_is_simply_used(fresh_app):
    from tests.conftest import seed_user
    uid = seed_user(name="Dirk", role="admin", email="d5@example.com")
    ids = _accounts(uid, "only@web.de")
    _out, actions = _prepare(uid)
    card = [a for a in actions if a["type"] == "email_ready"][0]
    assert card["account_id"] == ids["only@web.de"] and card["accounts"] == []


def test_sending_yoriks_draft_marks_its_card(fresh_app, monkeypatch):
    from tests.conftest import login_client
    from backend import email_routes
    from backend.agent import conversation_io as cio
    client, uid = login_client(fresh_app, role="admin", name="Dirk", email="d6@example.com")
    ids = _accounts(uid, "only@web.de")
    _out, actions = _prepare(uid)
    card = [a for a in actions if a["type"] == "email_ready"][0]
    cio.save_messages("conv-mail", "admin", uid, [
        {"role": "user", "content": "mail"},
        {"role": "assistant", "content": "Bereit.", "metadata": {"ui_actions": [card]}}])
    monkeypatch.setattr(email_routes, "send", lambda *a, **k: {"ok": True})
    body = {"account_id": ids["only@web.de"], "to": ["illtys@gmx.de"], "subject": "Kaffee", "body_text": "Hallo"}
    # another mail leaves the staged draft alone
    assert client.post("/api/email/send", json=body).status_code == 200
    assert client.get("/api/email/pending-draft").json()["draft"] is not None
    assert client.post("/api/email/send", json={**body, "from_yorik_draft": True}).status_code == 200
    assert client.get("/api/email/pending-draft").json()["draft"] is None
    stored = client.get("/api/conversations/conv-mail").json()["messages"][-1]["ui_actions"][0]
    assert stored["done"] == "sent"


def test_thinking_aloud_next_to_a_tool_call_is_not_a_bubble(fresh_app):
    from tests.conftest import login_client
    from backend.agent import conversation_io as cio
    client, uid = login_client(fresh_app, role="admin", name="Dirk", email="d7@example.com")
    cio.save_messages("conv-think", "admin", uid, [
        {"role": "user", "content": "trag naechsten donnerstag friseur ein"},
        {"role": "assistant", "content": "User said 'next Thursday' → lookup table → 2026-10-01",
         "tool_calls": [{"id": "1", "type": "function", "function": {"name": "add_calendar_event", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
        {"role": "assistant", "content": "Eingetragen."},
        {"role": "user", "content": "und dann?"},
        # a turn that ended on a tool call keeps its only text
        {"role": "assistant", "content": "Ich schaue nach …",
         "tool_calls": [{"id": "2", "type": "function", "function": {"name": "check_calendar", "arguments": "{}"}}]},
    ])
    shown = [m["content"] for m in client.get("/api/conversations/conv-think").json()["messages"]]
    assert shown == ["trag naechsten donnerstag friseur ein", "Eingetragen.", "und dann?", "Ich schaue nach …"]


def test_a_persons_number_and_lid_are_one_chat(fresh_app, monkeypatch):
    """Beate wrote under her LID, Dirk's reply went to her number: two
    chats for one person. The list and the thread show one."""
    from tests.conftest import login_client
    from backend.database import get_conn
    from backend import whatsapp_aliases as al
    client, uid = login_client(fresh_app, role="admin", name="Dirk", email="d8@example.com")
    pn, lid, other = "4917637995916@s.whatsapp.net", "64373087281262@lid", "4915100000000@s.whatsapp.net"
    with get_conn() as conn:
        for jid, name, src, ts, txt, unread in ((pn, "Beate <3", "book", 200, "Test von Yorik", 0),
                                                 (lid, "Beate <3", "push", 300, "Pumpe?", 2),
                                                 (other, "Tom", "book", 100, "Hi", 0)):
            conn.execute("INSERT INTO wa_chats (jid, name, name_source, is_group, owner_user_id, last_message_ts, "
                         "last_message_text, unread_count) VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
                         (jid, name, src, uid, ts, txt, unread))
            conn.execute("INSERT INTO wa_messages (msg_id, chat_jid, from_me, timestamp, text, owner_user_id) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (f"m-{ts}", jid, 1 if jid == pn else 0, ts, txt, uid))
        conn.commit()
    al.forget()
    monkeypatch.setattr(al, "_from_bridge", lambda user_id: {lid: pn})
    chats = client.get("/api/whatsapp/chats").json()
    assert [c["jid"] for c in chats] == [pn, other]
    beate = chats[0]
    assert set(beate["aliases"]) == {pn, lid}
    assert beate["last_message_text"] == "Pumpe?" and beate["unread_count"] == 2
    assert beate["name_source"] == "book"
    for jid in (pn, lid):
        texts = [m["text"] for m in client.get(f"/api/whatsapp/chats/{jid}/messages").json()]
        assert texts == ["Test von Yorik", "Pumpe?"]
    # a stopped bridge keeps the last good map
    al.forget()
    monkeypatch.setattr(al, "_from_bridge", lambda user_id: None)
    assert [c["jid"] for c in client.get("/api/whatsapp/chats").json()] == [pn, other]


def test_what_to_say_is_written_as_the_users_message():
    from backend.whatsapp import _build_draft_prompt
    recent = [{"from_me": 0, "push_name": "Beate", "text": "Hast du eine Pumpe?", "transcript": None}]
    with_intent = _build_draft_prompt("Beate <3", False, recent, [], extra="dass ich heute später nach Hause komme")
    assert "What the user wants to say" in with_intent and "\"I\" is the user" in with_intent
    assert with_intent.rstrip().endswith("Message:") and "Hast du eine Pumpe?" in with_intent
    plain = _build_draft_prompt("Beate <3", False, recent, [])
    assert plain.rstrip().endswith("Draft reply:") and "What the user wants" not in plain


def test_no_draft_while_the_contact_choice_is_open(fresh_app):
    import asyncio
    from backend.skills.whatsapp_draft.skill import execute
    from backend.skills.registry import Registry, SkillContext
    from backend.ui_tools import _append, reset_ui_actions
    from tests.conftest import seed_user
    uid = seed_user(name="Dirk", role="admin", email="d9@example.com")

    async def run(**kw):
        reset_ui_actions()
        _append({"type": "contact_picker", "query": "Dirk Winiecki",
                 "contacts": [{"id": 11, "display_name": "Dirk Winiecki", "whatsapp": "4915128811000@s.whatsapp.net"},
                              {"id": 12, "display_name": "Dirk Winiecki", "whatsapp": None}]})
        return await execute(ctx=SkillContext(Registry(), role="admin", user_id=uid), intent="test", **kw)
    for kw in ({"contact_id": 11}, {"chat_jid": "4915128811000@s.whatsapp.net"}):
        out = asyncio.run(run(**kw))
        assert out["ok"] is False and out["_llm_hint"].startswith("STOP") and out["drafts"] == []


def test_country_sets_currency_and_survives_in_the_database(fresh_app, monkeypatch, tmp_path):
    """b16: the country chosen in onboarding also decides the money the
    household counts in; in Docker the choice lives in the DB, not in a
    config.env that vanishes with the container."""
    from backend import locale as L
    from backend.household_settings import currency
    monkeypatch.setenv("HOMEOS_CONFIG_FILE", str(tmp_path / "config.env"))
    assert currency() == "EUR"
    out = L.apply_country("US")
    assert out["applied"] and out["currency"] == "USD" and out["tz"] == "America/New_York"
    assert currency() == "USD" and (tmp_path / "config.env").read_text().count("YORIK_TZ=America/New_York") == 1
    monkeypatch.setenv("YORIK_TZ", "Europe/Berlin")
    L.apply_saved()
    import os
    assert os.environ["YORIK_TZ"] == "America/New_York"
    # Docker: nothing written to config.env, the note says where OCR comes from
    monkeypatch.setenv("YORIK_RUNTIME", "docker")
    (tmp_path / "config.env").unlink()
    out = L.apply_country("CH")
    assert out["currency"] == "CHF" and "installer" in out["note"] and not (tmp_path / "config.env").exists()
    assert currency() == "CHF"
    assert L.apply_country("XX")["applied"] is False


def test_a_bill_without_currency_takes_the_households(fresh_app, monkeypatch, tmp_path):
    import asyncio
    from backend import locale as L
    from backend.skills._add_bill.skill import execute
    from backend.skills.registry import Registry, SkillContext
    from backend.database import get_conn
    from tests.conftest import seed_user
    from backend.payments import money
    monkeypatch.setenv("HOMEOS_CONFIG_FILE", str(tmp_path / "config.env"))
    uid = seed_user(name="Ann", role="admin", email="ann@example.com")
    L.apply_country("GB")
    asyncio.run(execute(ctx=SkillContext(Registry(), role="admin", user_id=uid), name="Water", amount=12.5,
                        due_date="2026-10-15"))
    with get_conn() as conn:
        row = conn.execute("SELECT currency FROM bills WHERE name = 'Water'").fetchone()
    assert row["currency"] == "GBP" and money(1250) == "£12.50"


def test_niche_skills_are_off_until_the_admin_says_otherwise(fresh_app):
    """s19 (Dirk 2026-09-30): the outside agent, the day planner, venue
    prices and PDF forms are not offered on a fresh install; Settings →
    Skills can turn each on, and that choice is what counts afterwards."""
    from tests.conftest import login_client
    from backend.skills import get_registry
    from backend.skills.registry import NICHE_OFF_BY_DEFAULT, get_admin_disabled_skills
    client, _ = login_client(fresh_app, role="admin", name="Dirk", email="dd@example.com")
    offered = {r["name"] for r in get_registry().index(role="admin")}
    assert not (NICHE_OFF_BY_DEFAULT & offered) and "check_calendar" in offered
    assert get_admin_disabled_skills() == set(NICHE_OFF_BY_DEFAULT)
    assert client.patch("/api/skills/plan_my_day", json={"enabled": True}).status_code == 200
    assert "plan_my_day" in {r["name"] for r in get_registry().index(role="admin")}
    assert get_admin_disabled_skills() == set(NICHE_OFF_BY_DEFAULT) - {"plan_my_day"}


def test_sharing_a_contact_waits_for_the_card(fresh_app):
    """s18 (Dirk 2026-09-30): sharing a contact and changing a document's
    visibility go through a confirmation card like a delete."""
    import asyncio
    from tests.conftest import login_client, seed_user
    from backend import contacts as C
    from backend.database import get_conn
    from backend.skills.share_contact.skill import execute
    from backend.skills.registry import Registry, SkillContext
    from backend.ui_tools import get_ui_actions, reset_ui_actions
    client, dirk = login_client(fresh_app, role="admin", name="Dirk", email="dk@example.com")
    beate = seed_user(name="Beate", role="member", email="be@example.com")
    cid = C.create(display_name="Zahnarzt Dr. Weber", kind="business", created_by_user_id=dirk)
    async def run():
        reset_ui_actions()
        out = await execute(ctx=SkillContext(Registry(), role="admin", user_id=dirk),
                            contact_id=cid, with_user_id=beate, can_edit=False)
        return out, get_ui_actions()
    out, actions = asyncio.run(run())
    assert out["pending"] and "NOTHING is shared yet" in out["_llm_hint"]
    card = [a for a in actions if a["type"] == "pending_confirmation"][0]
    assert card["preview"]["mode"] == "confirm_before" and card["preview"]["action"] == "share"
    assert card["preview"]["with_name"] == "Beate"
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM row_shares WHERE row_id = ?", (cid,)).fetchone()["n"] == 0
    r = client.post(f"/api/pending/{out['pending_id']}/confirm", json={})
    assert r.status_code == 200 and r.json()["applied"]["applied"] == "share_contact", r.text
    with get_conn() as conn:
        row = conn.execute("SELECT level FROM row_shares WHERE row_id = ? AND user_id = ?", (cid, beate)).fetchone()
    assert row and row["level"] == "read"


def test_untick_takes_the_spawned_routine_copy_back_and_the_board_hides_tomorrow(fresh_app):
    """Q49/Q36 (2026-10-01): a tick spawns tomorrow's instance; the board
    showed it at once, so children ticked the same routine four times.
    Untick removes the spawned copy; the board shows routines up to today."""
    from tests.conftest import login_client
    from datetime import date, timedelta
    client, uid = login_client(fresh_app, role="admin", name="Dirk", email="d10@example.com")
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET kiosk_agenda_consent = 1 WHERE id = ?", (uid,)); conn.commit()
    today = date.today().isoformat()
    r = client.post("/api/tasks?role=admin", json={"title": "Zähne putzen", "due_date": today, "recurrence_rule": "daily"})
    tid = r.json()["id"]
    assert client.patch(f"/api/tasks/{tid}?role=admin", json={"done": True}).status_code == 200
    rows = [t for t in client.get("/api/tasks?role=admin").json() if t["title"] == "Zähne putzen"]
    assert sorted((t["done"], t["due_date"][:10]) for t in rows) == [(0, (date.today() + timedelta(days=1)).isoformat()), (1, today)]
    # the board: today's done tick, not tomorrow's open copy
    board = client.get("/api/ambient/board").json()
    shown = [(t["title"], t["done"]) for t in board["tasks"] if t["title"] == "Zähne putzen"]
    assert shown == [("Zähne putzen", True)]
    # untick → the spawned copy is gone, one open task remains
    assert client.patch(f"/api/tasks/{tid}?role=admin", json={"done": False}).status_code == 200
    rows = [t for t in client.get("/api/tasks?role=admin").json() if t["title"] == "Zähne putzen"]
    assert [(t["done"], t["due_date"][:10]) for t in rows] == [(0, today)]


def test_starting_a_shared_task_leaves_the_other_persons_timer_alone(fresh_app):
    """Q38: Dirk starts a chore shared with Beate — Beate's own running
    task keeps running; only Dirk's other timer stops."""
    from tests.conftest import login_client, seed_user
    dirk_c, dirk = login_client(fresh_app, role="admin", name="Dirk", email="d11@example.com")
    beate = seed_user(name="Beate", role="member", email="b11@example.com")
    from backend import auth_sessions
    from fastapi.testclient import TestClient
    sid = auth_sessions.create_session(beate, user_agent="pytest", ip="127.0.0.1")
    beate_c = TestClient(fresh_app); beate_c.cookies.set(auth_sessions.COOKIE_NAME, sid)
    shared = dirk_c.post("/api/tasks?role=admin", json={"title": "Keller", "assignee_user_ids": [dirk, beate]}).json()["id"]
    hers = beate_c.post("/api/tasks?role=member", json={"title": "Steuer", "assignee_user_ids": [beate]}).json()["id"]
    his = dirk_c.post("/api/tasks?role=admin", json={"title": "Video", "assignee_user_ids": [dirk]}).json()["id"]
    assert beate_c.post(f"/api/tasks/{hers}/start?role=member").status_code == 200
    assert dirk_c.post(f"/api/tasks/{his}/start?role=admin").status_code == 200
    assert dirk_c.post(f"/api/tasks/{shared}/start?role=admin").status_code == 200
    rows = {t["title"]: t for t in dirk_c.get("/api/tasks?role=admin").json()}
    assert rows["Keller"]["started_at"] and rows["Video"]["started_at"] is None
    hers_rows = {t["title"]: t for t in beate_c.get("/api/tasks?role=member").json()}
    assert hers_rows["Steuer"]["started_at"]                 # Beate's timer untouched


def test_person_card_documents_must_name_the_person(fresh_app, monkeypatch):
    """Q24: the card's document list came from nearest-neighbour search
    with no floor — four unrelated documents for "Erbbaurecht"."""
    from backend import people_routes as P, paperless_ingest as PI
    hits = [
        {"doc_title": "Kobra Aufnahmeantrag", "text": "Kobra Kampfsport Peine", "distance": 0.61, "correspondent": None},
        {"doc_title": "Erbbauzins Mahnung", "text": "Sehr geehrter Herr … Erbbaurecht", "distance": 0.31, "correspondent": "Erbbaurecht"},
        {"doc_title": "Netcup invoice", "text": "server", "distance": 0.40, "correspondent": "netcup"},
    ]
    monkeypatch.setattr(PI, "search", lambda q, k=8, creds_override=None: hits)
    monkeypatch.setattr(PI, "semantic_max_distance", lambda: 0.55)
    monkeypatch.setattr("backend.external_users.get_user_paperless_creds", lambda uid: None)
    out = P._docs_by_names(["Erbbaurecht"], "u1")
    assert [h["doc_title"] for h in out] == ["Erbbauzins Mahnung"]


def test_geocoding_prefers_the_households_country_and_home_area(fresh_app, monkeypatch):
    """Q31: 'Dr. Mueller' resolved to the first Dr. Müller on earth."""
    from backend.connectors import maps
    from backend.database import get_conn
    from backend import locale as L
    from tests.conftest import seed_user
    uid = seed_user(name="Dirk", role="admin", email="d12@example.com")
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET address_street='Amselweg 1', address_postcode='31224', address_city='Peine' WHERE id=?", (uid,))
        conn.commit()
    L.remember(L.COUNTRY_LOCALE["DE"], "DE")
    calls = []
    class R:
        ok = True
        def __init__(self, payload): self._p = payload
        def json(self): return self._p
        def raise_for_status(self): pass
    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params or {}))
        if params.get("q", "").startswith("Amselweg"):
            return R([{"lat": "52.32", "lon": "10.23", "display_name": "Peine"}])
        return R([{"lat": "52.33", "lon": "10.24", "display_name": "Dr. Mueller, Peine", "type": "dentist"}])
    monkeypatch.setattr(maps.requests, "get", fake_get)
    maps._HOME_BIAS_AT = 0.0; maps._GEOCODE_CACHE.clear()
    hit = maps._geocode_one("Dr. Mueller")
    assert hit and "Peine" in hit["label"]
    q = [c for c in calls if c.get("q") == "Dr. Mueller"][0]
    assert q["countrycodes"] == "de" and q["bounded"] == 0 and q["viewbox"].startswith("9.2300,53.3200")


def test_two_invoices_at_once_get_two_numbers(fresh_app):
    """Q57: the second of two simultaneous allocations failed with an
    integrity error instead of getting the next number."""
    import threading
    from backend.compose import series as S
    from tests.conftest import seed_user
    uid = seed_user(name="Dirk", role="admin", email="d13@example.com")
    created = S.install_preset("de", owner_user_id=uid)
    sid = next(c["id"] for c in created if c["kind"] == "rechnung")
    got, errors = [], []
    def go():
        try:
            got.append(S.consume(sid, consumed_by_user_id=uid, title="x")["number"])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors, errors
    assert sorted(got) == list(range(min(got), min(got) + 4))


def test_whatsapp_photos_survive_a_bridge_restart(fresh_app, monkeypatch, tmp_path):
    """Photo (unavailable) after every bridge restart: the bridge keeps
    bytes only in memory. Yorik now keeps its own copy at ingest and the
    media route serves it before asking the bridge."""
    import asyncio
    from tests.conftest import login_client
    from backend import whatsapp_media as WM
    monkeypatch.setattr(WM, "MEDIA_DIR", str(tmp_path / "wa"))
    client, uid = login_client(fresh_app, role="admin", name="Dirk", email="d14@example.com")

    class FakeResp:
        status_code = 200
        content = b"\xff\xd8JPEGBYTES"
        headers = {"content-type": "image/jpeg"}
    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **k): return FakeResp()
    monkeypatch.setattr(WM.httpx, "AsyncClient", FakeClient)
    assert asyncio.run(WM.keep_local_copy("ABC123", str(uid), "image"))
    path, mime = WM.local_media_file(str(uid), "ABC123")
    assert path.endswith(".jpg") and mime == "image/jpeg"
    assert not asyncio.run(WM.keep_local_copy("VID1", str(uid), "video"))   # videos are not kept

    # the route serves the copy even when the bridge is gone
    from backend import whatsapp as W
    class DeadClient(FakeClient):
        async def get(self, url, **k): raise W.httpx.RequestError("down")
    monkeypatch.setattr(W.httpx, "AsyncClient", DeadClient)
    r = client.get("/api/whatsapp/media/ABC123")
    assert r.status_code == 200 and r.content == FakeResp.content and r.headers["content-type"].startswith("image/jpeg")
    assert client.get("/api/whatsapp/media/NOPE").status_code == 404


def test_a_weekday_or_tomorrow_in_a_mail_is_a_date():
    """e2e 'school letter becomes an appointment': 'Wandertag am Freitag'
    had no date for the calendar."""
    from datetime import date
    from backend.email_invites import extract_appointment
    today = date(2026, 10, 1)                       # a Thursday
    assert extract_appointment("Elternbrief: Wandertag am Freitag.", today=today)["date"] == "2026-10-02"
    assert extract_appointment("Donnerstag Elternabend", today=today)["date"] == "2026-10-08"   # next, not today
    assert extract_appointment("kommt ihr morgen um drei?", today=today)["date"] == "2026-10-02"
    assert extract_appointment("See you tomorrow at 9am", today=today) == {"date": "2026-10-02", "time": "09:00"}
    # a written date next to a weekday wins; "Guten Morgen" is not a day
    assert extract_appointment("Freitag, 3. Oktober um 14 Uhr", today=today)["date"] == "2026-10-03"
    assert "date" not in extract_appointment("Guten Morgen, die Rechnung", today=today)
