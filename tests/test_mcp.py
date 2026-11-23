"""MCP endpoint + personal API tokens.

The MCP surface is the skills registry for outside agents. These tests
pin the contract that matters: no cookie access, token-bound identity,
role-filtered tool list, skill execution under the owner's context, and
the confirm-before-apply hop.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import login_client


def _rpc(client, token, method, params=None, msg_id=1, mode="full"):
    """Most tests were written against the full surface (one tool per
    skill); compact is the default on the wire and gets its own tests."""
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    url = "/mcp?tools=compact" if mode == "compact" else "/mcp"
    return client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def token_client(fresh_app):
    """(anonymous client, plain token, user id) for a fresh member."""
    logged_in, uid = login_client(fresh_app, role="member")
    # Same bootstrap users.create_user does, so calendar skills have a target.
    from backend import spaces as _sp
    from backend.calendars import ensure_calendars_for_user
    _sp.ensure_personal_space(uid, "member-user")
    ensure_calendars_for_user(uid, "member-user")
    r = logged_in.post("/api/tokens", json={"name": "pytest agent"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert token.startswith("yk_")
    return TestClient(fresh_app), token, uid, logged_in


def test_mcp_rejects_cookie_and_anonymous(fresh_app):
    logged_in, _ = login_client(fresh_app, role="admin")
    r = logged_in.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    r = TestClient(fresh_app).post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Bearer")


def test_mcp_get_is_405(fresh_app):
    assert TestClient(fresh_app).get("/mcp").status_code == 405


def test_initialize_and_ping(token_client):
    client, token, _, _ = token_client
    r = _rpc(client, token, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    })
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["protocolVersion"] == "2025-03-26"
    assert body["serverInfo"]["name"] == "yorik"
    assert "tools" in body["capabilities"]
    assert r.headers.get("mcp-session-id")

    # notifications get a bare 202
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 202

    r = _rpc(client, token, "ping")
    assert r.json()["result"] == {}


def test_tools_list_is_role_filtered(token_client, fresh_app):
    client, token, _, _ = token_client
    names = {t["name"] for t in _rpc(client, token, "tools/list").json()["result"]["tools"]}
    assert {"skill_view", "pending_confirm", "pending_cancel", "whoami"} <= names
    assert {"check_calendar", "email_draft"} <= names

    # email_draft is [admin, member] in its manifest; restricted must not see it.
    # (Compose, used here until 2026-09-29, is retired.)
    restricted, _ = login_client(fresh_app, role="restricted", email="rst@example.local")
    r_token = restricted.post("/api/tokens", json={"name": "x"}).json()["token"]
    r_names = {t["name"] for t in _rpc(client, r_token, "tools/list").json()["result"]["tools"]}
    assert "check_calendar" in r_names
    assert "email_draft" not in r_names
    r = _rpc(client, r_token, "tools/call", {"name": "email_draft", "arguments": {}})
    assert r.json()["error"]["code"] == -32602

    # schemas come from skill.md inputs
    tools = {t["name"]: t for t in _rpc(client, token, "tools/list").json()["result"]["tools"]}
    schema = tools["add_calendar_event"]["inputSchema"]
    assert schema["type"] == "object"
    assert {"title", "starts_at"} <= set(schema["required"])
    assert schema["properties"]["starts_at"]["type"] == "string"


def test_whoami_and_skill_view(token_client):
    client, token, uid, _ = token_client
    r = _rpc(client, token, "tools/call", {"name": "whoami", "arguments": {}})
    body = r.json()["result"]
    assert body["isError"] is False
    assert body["structuredContent"]["id"] == uid
    assert body["structuredContent"]["role"] == "member"

    r = _rpc(client, token, "tools/call",
             {"name": "skill_view", "arguments": {"name": "check_calendar"}})
    view = r.json()["result"]["structuredContent"]
    assert view["name"] == "check_calendar"
    assert "body" in view


def test_unknown_tool_is_protocol_error(token_client):
    client, token, _, _ = token_client
    r = _rpc(client, token, "tools/call", {"name": "install_connector", "arguments": {}})
    assert r.json()["error"]["code"] == -32602
    r = _rpc(client, token, "tools/call", {"name": "no_such_skill", "arguments": {}})
    assert r.json()["error"]["code"] == -32602


def test_bad_args_are_tool_errors(token_client):
    client, token, _, _ = token_client
    r = _rpc(client, token, "tools/call",
             {"name": "check_calendar", "arguments": {"bogus_arg": 1}})
    body = r.json()["result"]
    assert body["isError"] is True
    assert "bogus_arg" in body["content"][0]["text"]


def test_create_then_delete_round_trip(token_client):
    """add_calendar_event runs as the token owner; delete stages a
    confirm-before card that pending_confirm resolves."""
    client, token, uid, logged_in = token_client
    r = _rpc(client, token, "tools/call", {
        "name": "add_calendar_event",
        "arguments": {"title": "MCP dentist", "starts_at": "2030-01-15T10:00:00"},
    })
    body = r.json()["result"]
    assert body["isError"] is False, body
    created = body["structuredContent"]["result"]
    event_id = created.get("event_id") or created.get("id") or (created.get("event") or {}).get("id")
    assert event_id, created

    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT owner_user_id FROM events WHERE id=?", (event_id,)).fetchone()
    assert str(row["owner_user_id"]) == str(uid)

    r = _rpc(client, token, "tools/call",
             {"name": "delete_calendar_event", "arguments": {"event_id": int(event_id)}})
    body = r.json()["result"]
    assert body["isError"] is False, body
    pending = body["structuredContent"]["pending_confirmation"]
    assert pending["preview"]["mode"] == "confirm_before"
    assert "Yorik app" in pending["next"]
    # Not the chat's "a confirmation card is on screen": no card shows here.
    assert "on screen" not in __import__("json").dumps(body["structuredContent"])
    # The client gets the question as a card: confirmed in Yorik, so the
    # only button withdraws it.
    ask = next(c for c in body["structuredContent"]["cards"] if c["type"] == "confirm")
    assert ask["uid"] == "p-" + pending["pending_id"] and "MCP dentist" in ask["text"] and "15.01." in ask["text"]
    assert [a["id"] for a in ask["actions"]] == ["cancel"] and pending["pending_id"] in ask["actions"][0]["say"]
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone()

    # The owner gets a card in the bell; the agent may not confirm.
    bell = logged_in.get("/api/notifications").json()["notifications"]
    card = next(n for n in bell if n["kind"] == "agent_pending")
    assert card["payload"]["pending_id"] == pending["pending_id"]
    assert "pytest agent" in card["title"] and "MCP dentist" in card["title"]

    r = _rpc(client, token, "tools/call",
             {"name": "pending_confirm", "arguments": {"pending_id": pending["pending_id"]}})
    assert r.json()["result"]["isError"] is True
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone()

    # A token cannot grant itself the right; the browser session can.
    r = client.patch("/api/profile/agent-deletes", json={"enabled": True},
                     headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    r = logged_in.patch("/api/profile/agent-deletes", json={"enabled": True})
    assert r.status_code == 200
    assert logged_in.get("/api/auth/me").json()["user"]["agent_may_confirm_deletes"] is True

    r = _rpc(client, token, "tools/call",
             {"name": "pending_confirm", "arguments": {"pending_id": pending["pending_id"]}})
    assert r.json()["result"]["isError"] is False, r.text
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone() is None
    # The question card is closed.
    done = r.json()["result"]["structuredContent"]["cards"]
    assert done == [{"type": "done", "card_uid": "p-" + pending["pending_id"], "text": "Deleted"}]

    # With the owner's leave, the card asks right away: Delete or Cancel.
    r = _rpc(client, token, "tools/call", {"name": "add_calendar_event", "arguments": {
        "title": "MCP barber", "starts_at": "2030-01-16T10:00:00"}})
    second = r.json()["result"]["structuredContent"]["result"]
    second_id = second.get("event_id") or second.get("id") or (second.get("event") or {}).get("id")
    r = _rpc(client, token, "tools/call", {"name": "delete_calendar_event", "arguments": {"event_id": int(second_id)}})
    sc = r.json()["result"]["structuredContent"]
    ask = next(c for c in sc["cards"] if c["type"] == "confirm")
    assert [a["label"] for a in ask["actions"]] == ["Delete", "Cancel"]
    assert "pending_confirm" in sc["pending_confirmation"]["next"] and "Already applied" not in sc["pending_confirmation"]["next"]
    r = _rpc(client, token, "tools/call", {"name": "pending_cancel", "arguments": {
        "pending_id": sc["pending_confirmation"]["pending_id"]}})
    assert r.json()["result"]["structuredContent"]["cards"][0]["text"] == "Not deleted"
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM events WHERE id=?", (second_id,)).fetchone()


def test_an_agent_deletes_one_thing_per_yes_and_not_many_in_a_row(token_client, monkeypatch):
    """With the owner's leave an agent confirms deletions, but one item per
    yes (a series waits for the owner) and at most one a minute."""
    from backend import mcp_server
    from backend.database import get_conn
    client, token, uid, logged_in = token_client
    assert logged_in.patch("/api/profile/agent-deletes", json={"enabled": True}).status_code == 200

    def add(title, day):
        r = _rpc(client, token, "tools/call", {"name": "add_calendar_event", "arguments": {
            "title": title, "starts_at": f"2030-02-{day:02d}T10:00:00"}})
        res = r.json()["result"]["structuredContent"]["result"]
        return int(res.get("event_id") or res.get("id") or (res.get("event") or {}).get("id"))

    def stage(event_id):
        r = _rpc(client, token, "tools/call", {"name": "delete_calendar_event", "arguments": {"event_id": event_id}})
        return r.json()["result"]["structuredContent"]["pending_confirmation"]["pending_id"]

    def confirm(pid):
        return _rpc(client, token, "tools/call", {"name": "pending_confirm", "arguments": {"pending_id": pid}}
                    ).json()["result"]

    first, second = add("One", 1), add("Two", 2)
    assert confirm(stage(first))["isError"] is False
    out = confirm(stage(second))
    assert out["isError"] is True and "a minute" in str(out)                 # not two in a row
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM events WHERE id=?", (second,)).fetchone()

    # A deletion that takes more along waits for the owner, however long ago the last was.
    monkeypatch.setattr(mcp_server, "_agent_deletion_times", lambda user: [])
    from backend import pending_actions as _pa
    pid = stage(second)
    with get_conn() as conn:
        conn.execute("UPDATE pending_actions SET rollback_args_json = ? WHERE id = ?",
                     ('{"event_id": %d, "linked_ids": [1, 2]}' % second, pid))
        conn.commit()
    assert _pa.get(pid)["rollback_args"]["linked_ids"] == [1, 2]
    out = confirm(pid)
    assert out["isError"] is True and "more than one item" in str(out)


def test_owner_deletes_from_the_bell(token_client):
    """Default policy: the card in the bell runs the deletion."""
    client, token, uid, logged_in = token_client
    r = _rpc(client, token, "tools/call", {
        "name": "add_calendar_event",
        "arguments": {"title": "Bell test", "starts_at": "2030-02-01T09:00:00"},
    })
    created = r.json()["result"]["structuredContent"]["result"]
    event_id = created.get("event_id") or created.get("id") or (created.get("event") or {}).get("id")
    r = _rpc(client, token, "tools/call",
             {"name": "delete_calendar_event", "arguments": {"event_id": int(event_id)}})
    pending_id = r.json()["result"]["structuredContent"]["pending_confirmation"]["pending_id"]
    r = logged_in.post(f"/api/pending/{pending_id}/confirm")
    assert r.status_code == 200, r.text
    from backend.database import get_conn
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone() is None


def test_token_lifecycle(token_client):
    client, token, uid, logged_in = token_client
    listed = logged_in.get("/api/tokens").json()
    assert len(listed) == 1 and listed[0]["prefix"].startswith("yk_") and not listed[0]["revoked"]
    assert "token" not in listed[0]

    # a token may drive /api/* too, but must not mint tokens
    r = client.get("/api/tokens", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    r = client.post("/api/tokens", json={"name": "nested"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403

    r = logged_in.delete(f"/api/tokens/{listed[0]['id']}")
    assert r.status_code == 200
    assert _rpc(client, token, "ping").status_code == 401
    assert logged_in.get("/api/tokens").json()[0]["revoked"] is True


def test_token_calls_are_audited_and_admins_see_all_tokens(token_client, fresh_app):
    client, token, uid, logged_in = token_client
    _rpc(client, token, "tools/call", {"name": "check_calendar", "arguments": {"days": 1}})
    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT source FROM skill_invocations WHERE skill_id='check_calendar' AND user_id=? "
            "ORDER BY id DESC LIMIT 1", (uid,),
        ).fetchone()
    assert row and row["source"] == "token:pytest agent"

    # a member cannot list the household's tokens, an admin can
    assert logged_in.get("/api/tokens/all").status_code == 403
    admin, _ = login_client(fresh_app, role="admin", email="adm2@example.local")
    rows = admin.get("/api/tokens/all").json()
    mine = [r for r in rows if r["owner_id"] == str(uid)]
    assert mine and mine[0]["name"] == "pytest agent" and "token" not in mine[0]
    assert admin.delete(f"/api/tokens/{mine[0]['id']}").status_code == 200
    assert _rpc(client, token, "ping").status_code == 401


def test_compact_mode_collapses_skills_behind_invoke_skill(token_client):
    client, token, uid, _ = token_client
    r = _rpc(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "0"}}, mode="compact")
    assert "invoke_skill(name, args) directly" in r.json()["result"]["instructions"]
    assert "invoke_skill" not in _rpc(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "0"}}).json()["result"]["instructions"]
    tools = _rpc(client, token, "tools/list", mode="compact").json()["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["skill_view", "pending_confirm", "pending_cancel", "whoami", "notify", "invoke_skill", "list_skills"]
    inv = next(t for t in tools if t["name"] == "invoke_skill")
    assert "- check_calendar:" in inv["description"] and "[calendar]" in inv["description"]
    assert "email_draft" in inv["description"]             # member may use it
    assert "compose_draft" not in inv["description"]        # retired
    assert "- ask_agent:" not in inv["description"]         # no-mcp skill stays hidden
    # compact is much smaller than full
    import json
    full = _rpc(client, token, "tools/list", mode="full").json()["result"]["tools"]
    assert len(json.dumps(tools)) < len(json.dumps(full)) / 3

    # direct invoke works; a wrong argument name answers with the valid keys
    r = _rpc(client, token, "tools/call", {"name": "invoke_skill", "arguments": {"name": "check_calendar", "args": {"days": 3}}}, mode="compact")
    body = r.json()["result"]
    assert body["isError"] is False, body
    assert "result" in body["structuredContent"]
    r = _rpc(client, token, "tools/call", {"name": "invoke_skill", "arguments": {"name": "check_calendar", "args": {"range": "week"}}}, mode="compact")
    body = r.json()["result"]
    assert body["isError"] is True and "AVAILABLE KEYS" in body["content"][0]["text"]
    # optional read-first gate for weaker models
    import os
    os.environ["YORIK_MCP_REQUIRE_VIEW"] = "1"
    try:
        r = _rpc(client, token, "tools/call", {"name": "invoke_skill", "arguments": {"name": "add_task", "args": {"title": "x"}}}, mode="compact")
        body = r.json()["result"]
        assert body["isError"] is True and "skill_view(name='add_task')" in body["content"][0]["text"]
        _rpc(client, token, "tools/call", {"name": "skill_view", "arguments": {"name": "add_task"}}, mode="compact")
        r = _rpc(client, token, "tools/call", {"name": "invoke_skill", "arguments": {"name": "add_task", "args": {"title": "x"}}}, mode="compact")
        assert r.json()["result"]["isError"] is False, r.json()["result"]
    finally:
        os.environ.pop("YORIK_MCP_REQUIRE_VIEW", None)
    # unknown skill, wrong args shape
    r = _rpc(client, token, "tools/call", {"name": "invoke_skill", "arguments": {"name": "nope", "args": {}}}, mode="compact")
    assert r.json()["result"]["isError"] is True
    r = _rpc(client, token, "tools/call", {"name": "list_skills", "arguments": {}}, mode="compact")
    assert "- add_task:" in r.json()["result"]["structuredContent"]["skills"]
    # full mode does not gate
    r = _rpc(client, token, "tools/call", {"name": "check_tasks", "arguments": {}}, mode="full")
    assert r.json()["result"]["isError"] is False, r.json()["result"]


def test_an_agent_gets_the_rows_the_chat_would_show_as_cards(token_client, fresh_app):
    """e2e 'an agent reads her tasks' failed: check_tasks told the agent
    only '10 open tasks — rendered as cards' and no titles."""
    client, token, uid, _ = token_client
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("INSERT INTO tasks (title, done, created_by_user_id, due_date) VALUES ('Steuererklärung abgeben', 0, ?, '2030-01-05')", (uid,))
        tid = conn.execute("SELECT id FROM tasks WHERE title = 'Steuererklärung abgeben'").fetchone()["id"]
        conn.execute("INSERT INTO task_assignees (task_id, user_id) VALUES (?, ?)", (tid, uid))
        conn.commit()
    r = _rpc(client, token, "tools/call", {"name": "check_tasks", "arguments": {"include_undated": True}})
    assert r.status_code == 200, r.text
    assert "Steuererklärung abgeben" in r.text
    body = r.json()["result"]["structuredContent"]
    assert body["cards"] and "If your client shows cards" in body["note"]
    # The chat's "say 'see the cards below', list nothing" does not reach the agent.
    assert "_llm_hint" not in body["result"] and "rendered as cards" not in r.text


def test_an_agent_sends_the_prepared_email_only_to_the_staged_recipient(token_client, fresh_app, monkeypatch):
    """Brepper shows the email card with a Send button; pressing it tells the
    agent to call send_prepared_email, which sends exactly the staged draft."""
    import json as _json

    client, token, uid, _ = token_client
    from backend import email_addresses, email_sender
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                     "smtp_username, credential_key, enabled, is_default) VALUES (?, 'me@mail.example', 'imap.x', "
                     "'me', 'smtp.x', 'me', 'k', 1, 1)", (uid,))
        conn.commit()
    monkeypatch.setattr(email_addresses, "known", lambda *a, **k: {"nina@example.com"})
    sent = []
    monkeypatch.setattr(email_sender, "send", lambda *a, **k: sent.append(a) or {"ok": True, "message_id": "<m>"})

    r = _rpc(client, token, "tools/call", {"name": "prepare_email", "arguments": {
        "to": "nina@example.com", "subject": "Danke", "body": "Hallo Nina, danke!"}})
    body = r.json()["result"]["structuredContent"]
    card = next(c for c in body["cards"] if c["type"] == "email_ready")
    assert card["actions"][0]["id"] == "send" and "nina@example.com" in card["actions"][0]["say"]
    assert card["body"] == "Hallo Nina, danke!" and card["from"] == "me@mail.example"   # to change it by hand
    assert sent == []                                         # preparing sends nothing

    # Another recipient than the staged one: nothing goes out.
    r = _rpc(client, token, "tools/call", {"name": "send_prepared_email", "arguments": {"to": "x@example.com"}})
    assert r.json()["result"]["structuredContent"]["result"]["sent"] is False and sent == []

    r = _rpc(client, token, "tools/call", {"name": "send_prepared_email", "arguments": {"to": "Nina@Example.com"}})
    result = r.json()["result"]["structuredContent"]["result"]
    assert result["sent"] is True and len(sent) == 1
    account_id, recipients, subject, text = sent[0][:4]
    assert recipients == ["nina@example.com"] and subject == "Danke" and text == "Hallo Nina, danke!"
    # The client learns that the card's email went out (its Send button goes).
    done = r.json()["result"]["structuredContent"]["cards"]
    assert done == [{"type": "email_sent", "to": "nina@example.com", "subject": "Danke",
                     "card_uid": card["uid"], "uid": done[0]["uid"]}]
    # Sent once: the staged draft is gone, a second press sends nothing and
    # says it went out already, so the agent does not send it again.
    r = _rpc(client, token, "tools/call", {"name": "send_prepared_email", "arguments": {"to": "nina@example.com"}})
    again = r.json()["result"]["structuredContent"]
    assert again["result"]["sent"] is False and again["result"]["already_sent"] is True and len(sent) == 1
    assert "do not prepare or send it again" in again["result"]["_llm_hint"]
    assert again["cards"][0]["type"] == "email_sent" and again["cards"][0]["card_uid"] == card["uid"]

    # An agent that no longer saw it went out prepares the same email anew
    # and sends it: still nothing goes out twice.
    r = _rpc(client, token, "tools/call", {"name": "prepare_email", "arguments": {
        "to": "nina@example.com", "subject": "Danke", "body": "Hallo Nina, danke!"}})
    anew = next(c for c in r.json()["result"]["structuredContent"]["cards"] if c["type"] == "email_ready")
    r = _rpc(client, token, "tools/call", {"name": "send_prepared_email", "arguments": {"to": "nina@example.com"}})
    twice = r.json()["result"]["structuredContent"]
    assert twice["result"]["already_sent"] is True and len(sent) == 1
    assert twice["cards"][0]["card_uid"] == anew["uid"]
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (f"pending_email_draft_{uid}",)).fetchone() is None
    assert _json.dumps(card)                                   # the card is plain JSON for any client


def test_the_same_draft_prepared_again_keeps_its_card(token_client, monkeypatch):
    """Send on the card the person pressed closes that card, also when the
    agent prepared the same email once more before sending it."""
    client, token, uid, _ = token_client
    from backend import email_addresses
    monkeypatch.setattr(email_addresses, "known", lambda *a, **k: {"nina@example.com"})
    args = {"to": "nina@example.com", "subject": "Danke", "body": "Hallo Nina, danke!"}

    def card():
        r = _rpc(client, token, "tools/call", {"name": "prepare_email", "arguments": args})
        return next(c for c in r.json()["result"]["structuredContent"]["cards"] if c["type"] == "email_ready")
    first, same = card(), card()
    assert same["uid"] == first["uid"] and same["replaced_previous"] is False
    args["body"] = "Hallo Nina, vielen Dank!"
    assert card()["uid"] != first["uid"]                       # another text is another email


def test_yoriks_own_chat_sends_from_the_composer_not_this_skill(fresh_app):
    import asyncio

    from backend.skills import SkillContext, get_registry
    reg = get_registry()
    out = asyncio.run(reg.invoke("send_prepared_email", ctx=SkillContext(reg, role="member", user_id=1, source="chat"),
                                 to="nina@example.com"))
    assert out["sent"] is False and "Send button" in out["_llm_hint"]


def _mail_with_attachments(uid, names):
    from backend.database import get_conn
    with get_conn() as conn:
        aid = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                           "smtp_username, credential_key) VALUES (?, 'me@mail.example', 'i', 'me', 's', 'me', 'k') "
                           "RETURNING id", (uid,)).fetchone()["id"]
        mid = conn.execute("INSERT INTO email_messages (account_id, uid, owner_user_id, message_id, from_email, "
                           "from_name, subject, body_text, has_attachments) VALUES (?, 1, ?, '<inv@x>', "
                           "'billing@vendor.example', 'Vendor', 'Payment received', 'Thanks', 1) RETURNING id",
                           (aid, uid)).fetchone()["id"]
        ids = [conn.execute("INSERT INTO email_attachments (message_id, filename, mimetype, size_bytes, "
                            "paperless_state) VALUES (?, ?, 'application/pdf', 10, 'suggested') RETURNING id",
                            (mid, n)).fetchone()["id"] for n in names]
        conn.commit()
    return mid, ids


def test_an_agent_files_an_email_attachment_and_gets_a_view_link(token_client, fresh_app, monkeypatch):
    """Asked to put an invoice from a mail into Paperless, an agent had no
    tool and went around Yorik (2026-11-19). Now it has one, and Brepper
    gets a View button that opens the file without a login."""
    client, token, uid, _ = token_client
    mid, (inv, rec) = _mail_with_attachments(uid, ["Invoice-ZK-3.pdf", "Receipt-ZK-3.pdf"])
    filed = []
    from backend import email_routes
    monkeypatch.setattr(email_routes, "file_attachment",
                        lambda att_id, user_id, vis=None: filed.append((att_id, vis)) or
                        {"ok": True, "state": "filed", "visibility": vis or "private"})
    monkeypatch.setattr("backend.email_actions.fetch_attachment_binary",
                        lambda att_id, user_id: {"content": b"%PDF-1.4 x", "mimetype": "application/pdf",
                                                 "filename": "Invoice-ZK-3.pdf"} if att_id == inv else None)

    # read_email lists the attachments with their ids and a View button each.
    r = _rpc(client, token, "tools/call", {"name": "read_email", "arguments": {"message_id": mid}})
    body = r.json()["result"]["structuredContent"]
    assert [a["filename"] for a in body["result"]["message"]["attachments"]] == ["Invoice-ZK-3.pdf", "Receipt-ZK-3.pdf"]
    card = next(c for c in body["cards"] if c["type"] == "attachments_found")
    assert [a["label"] for a in card["actions"]] == ["View: Invoice-ZK-3.pdf", "View: Receipt-ZK-3.pdf"]

    # Two attachments and no name: the agent is told to pick, nothing is filed.
    r = _rpc(client, token, "tools/call", {"name": "file_email_attachment", "arguments": {"message_id": mid}})
    res = r.json()["result"]["structuredContent"]["result"]
    assert res["ok"] is False and res["attachments"] == ["Invoice-ZK-3.pdf", "Receipt-ZK-3.pdf"] and filed == []

    r = _rpc(client, token, "tools/call", {"name": "file_email_attachment", "arguments": {
        "message_id": mid, "filename": "invoice", "visibility": "private"}})
    body = r.json()["result"]["structuredContent"]
    assert body["result"]["ok"] is True and filed == [(inv, "private")]
    card = next(c for c in body["cards"] if c["type"] == "document_filed")
    link = card["actions"][0]["url"]
    path = link[link.index("/api/file-links/"):]

    # The link opens that one file, with no session, shown in place.
    got = TestClient(fresh_app).get(path)
    assert got.status_code == 200 and got.content.startswith(b"%PDF")
    assert got.headers["content-disposition"].startswith("inline")
    # Tampered or for another file: nothing.
    assert TestClient(fresh_app).get(path[:-3] + "AAA").status_code == 404

    # Another person's mail is not theirs to file.
    r = _rpc(client, token, "tools/call", {"name": "file_email_attachment", "arguments": {"message_id": mid + 999}})
    assert r.json()["result"]["structuredContent"]["result"]["ok"] is False


def test_file_links_expire_and_only_pdfs_and_pictures_show_in_place(fresh_app, monkeypatch):
    from backend import file_links
    tok = file_links.mint("u1", "email_attachment", 5, now=1000)
    assert file_links.verify(tok, now=1000 + 60)["i"] == 5
    assert file_links.verify(tok, now=1000 + file_links.TTL_SECONDS + 1) is None
    monkeypatch.setattr("backend.email_actions.fetch_attachment_binary",
                        lambda att_id, user_id: {"content": b"<script>x</script>", "mimetype": "text/html",
                                                 "filename": "page.html"})
    got = TestClient(fresh_app).get(file_links.PREFIX + file_links.mint("u1", "email_attachment", 5))
    assert got.status_code == 200 and got.headers["content-disposition"].startswith("attachment")


def test_a_file_link_does_not_look_like_a_jwt(fresh_app):
    """Hermes hides JWT-like strings in tool results ("eyJ1Ij...5huo"), so a
    link that looked like one opened nothing in Brepper (2026-11-23)."""
    import base64
    import hashlib
    import hmac
    import json as _json
    import re
    from backend import file_links
    jwt = re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}")   # Hermes' agent/redact.py
    for n in range(300):
        tok = file_links.mint("7f0c2a1e-1111-2222-3333-444455556666", "document", n, now=1000 + n)
        assert not jwt.search(tok) and file_links.verify(tok, now=1000 + n)["i"] == n
    # A link minted before still opens until it runs out.
    body = _json.dumps({"u": "u1", "k": "document", "i": 3, "e": 5000}, separators=(",", ":")).encode()
    sig = hmac.new(file_links._secret(), body, hashlib.sha256).digest()[:18]
    old = f"{file_links._b64(body)}.{file_links._b64(sig)}"
    assert old.startswith("eyJ") and file_links.verify(old, now=1000)["i"] == 3


def test_a_pdf_the_mail_server_calls_octet_stream_still_shows_in_place(fresh_app, monkeypatch):
    """Supabase's invoice came as application/octet-stream (2026-11-20):
    the browser downloaded it instead of showing it."""
    from backend import file_links
    monkeypatch.setattr("backend.email_actions.fetch_attachment_binary",
                        lambda att_id, user_id: {"content": b"%PDF-1.7 x", "mimetype": "application/octet-stream",
                                                 "filename": "Receipt-ZK-3.pdf"})
    got = TestClient(fresh_app).get(file_links.PREFIX + file_links.mint("u1", "email_attachment", 5))
    assert got.headers["content-type"] == "application/pdf"
    assert got.headers["content-disposition"].startswith("inline")
    assert file_links.mime_of({"mimetype": "application/octet-stream", "filename": "setup.exe"}) != "application/pdf"


def test_found_documents_open_by_their_title_for_an_outside_agent(token_client, fresh_app, monkeypatch):
    """Brepper lists what search_documents / read_document found; each
    title opens that document (Paperless through the person's own token,
    an own upload from Yorik's copy)."""
    client, token, uid, _ = token_client
    from backend import file_links, paperless_ingest
    monkeypatch.setattr(paperless_ingest, "user_creds", lambda user_id: {"base_url": "http://pl", "api_key": "k"})
    monkeypatch.setattr(paperless_ingest, "_fetch_doc", lambda doc_id, creds_override=None: {"title": "Invoice 421"})
    monkeypatch.setattr(file_links, "_paperless_document",
                        lambda doc_id, user_id: {"content": b"%PDF-1.4 d", "mimetype": "application/pdf",
                                                 "filename": "invoice.pdf"} if doc_id == 421 and user_id == uid else None)
    from backend.documents import get_docs_conn, DOCS_DB_PATH
    with get_docs_conn(DOCS_DB_PATH) as conn:
        conn.execute("INSERT INTO paperless_chunks (paperless_doc_id, chunk_index, text) VALUES (421, 0, 'Pro plan 25 USD')")
        conn.commit()
    r = _rpc(client, token, "tools/call", {"name": "read_document", "arguments": {"doc_id": 421}})
    body = r.json()["result"]["structuredContent"]
    card = next(c for c in body["cards"] if c["type"] == "documents_found")
    link = card["documents"][0]["url"]
    got = TestClient(fresh_app).get(link[link.index(file_links.PREFIX):])
    assert got.status_code == 200 and got.content.startswith(b"%PDF")
    assert got.headers["content-disposition"].startswith("inline")
    # Another person's token for the same number opens nothing.
    assert TestClient(fresh_app).get(file_links.PREFIX + file_links.mint("someone-else", "document", 421)).status_code == 404


def test_a_drafted_letter_opens_as_its_pdf_for_an_outside_agent(token_client, fresh_app, monkeypatch):
    """Brepper cannot open the Writing app: the letter's card brings a
    button that shows the sheet as a PDF for 30 minutes."""
    client, token, uid, _ = token_client
    from backend import file_links
    from backend import apps
    from backend.writing import routes
    apps.set_opt_in_enabled("write", True)
    monkeypatch.setattr(routes, "_final_pdf", lambda doc, user_id: b"%PDF-1.4 letter")
    r = _rpc(client, token, "tools/call", {"name": "write_letter", "arguments": {
        "recipient": "Stadtwerke", "subject": "Kündigung", "text": "Sehr geehrte Damen und Herren, ..."}})
    body = r.json()["result"]["structuredContent"]
    card = next(c for c in body["cards"] if c["type"] == "writing_draft_created")
    link = card["actions"][0]["url"]
    got = TestClient(fresh_app).get(link[link.index(file_links.PREFIX):])
    assert got.status_code == 200 and got.content.startswith(b"%PDF")
    assert got.headers["content-disposition"].startswith("inline")
    other = file_links.mint(str(__import__("uuid").uuid4()), "writing", card["document_id"])
    assert TestClient(fresh_app).get(file_links.PREFIX + other).status_code == 404


def test_a_photo_link_names_one_asset_and_nothing_else(fresh_app, monkeypatch):
    from backend import file_links
    monkeypatch.setattr(file_links, "_photo", lambda asset_id, user_id: {
        "content": b"\xff\xd8\xff jpeg", "mimetype": "image/jpeg", "filename": "a.jpeg"} if asset_id == "abc-123" else None)
    got = TestClient(fresh_app).get(file_links.PREFIX + file_links.mint("u1", "photo", "abc-123"))
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    assert got.headers["content-disposition"].startswith("inline")
    with pytest.raises(ValueError):
        file_links.mint("u1", "photo", "../api/x")


def test_an_unknown_recipient_brings_a_button_per_known_address(monkeypatch):
    from backend import file_links
    from backend.skills.prepare_email import skill
    monkeypatch.setattr(file_links, "german", lambda user_id: True)
    actions = skill._pick_actions("u1", ["anna@example.org", "", None])
    assert [a["label"] for a in actions] == ["anna@example.org"]
    assert "anna@example.org" in actions[0]["say"] and "Empfänger" in actions[0]["say"]


def test_an_agent_deletes_one_email_after_a_yes(token_client, monkeypatch):
    """delete_email stages one email; the yes moves it to the Trash, through
    the same question card and limits as every agent deletion."""
    from backend import email_actions
    client, token, uid, logged_in = token_client
    mid, _ = _mail_with_attachments(uid, [])
    moved = []
    monkeypatch.setattr(email_actions, "delete_message", lambda m, u: moved.append((m, u)) or True)

    r = _rpc(client, token, "tools/call", {"name": "delete_email", "arguments": {"message_id": mid}})
    sc = r.json()["result"]["structuredContent"]
    ask = next(c for c in sc["cards"] if c["type"] == "confirm")
    assert "Payment received" in ask["text"] and "billing@vendor.example" in ask["text"] and moved == []
    assert logged_in.patch("/api/profile/agent-deletes", json={"enabled": True}).status_code == 200
    r = _rpc(client, token, "tools/call", {"name": "pending_confirm", "arguments": {
        "pending_id": sc["pending_confirmation"]["pending_id"]}})
    assert r.json()["result"]["isError"] is False and moved == [(mid, str(uid))]

    # Somebody else's email, or a list: nothing is staged.
    r = _rpc(client, token, "tools/call", {"name": "delete_email", "arguments": {"message_id": mid + 999}})
    assert r.json()["result"]["isError"] is True
    r = _rpc(client, token, "tools/call", {"name": "delete_email", "arguments": {"message_id": [mid, mid]}})
    assert r.json()["result"]["isError"] is True


def test_a_reminder_can_be_listed_and_cancelled_but_only_ones_own(token_client):
    import datetime as dt
    import uuid
    from backend import reminders
    client, token, uid, _ = token_client
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    mine = reminders.create(str(uid), "Zahnarzt anrufen", soon)
    theirs = reminders.create(str(uuid.uuid4()), "Fremde Erinnerung", soon)

    r = _rpc(client, token, "tools/call", {"name": "cancel_reminder", "arguments": {}})
    listed = r.json()["result"]["structuredContent"]["result"]["reminders"]
    assert [x["title"] for x in listed] == ["Zahnarzt anrufen"]                 # only one's own
    r = _rpc(client, token, "tools/call", {"name": "cancel_reminder", "arguments": {"reminder_id": theirs}})
    assert r.json()["result"]["structuredContent"]["result"]["cancelled"] is False
    r = _rpc(client, token, "tools/call", {"name": "cancel_reminder", "arguments": {"reminder_id": mine}})
    assert r.json()["result"]["structuredContent"]["result"]["cancelled"] is True
    r = _rpc(client, token, "tools/call", {"name": "cancel_reminder", "arguments": {}})
    assert r.json()["result"]["structuredContent"]["result"]["reminders"] == []
