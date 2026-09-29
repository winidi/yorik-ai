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
