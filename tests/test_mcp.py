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


def _rpc(client, token, method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})


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
    assert {"check_calendar", "compose_draft"} <= names

    # compose_draft is [admin, member] in its manifest; restricted must not see it.
    restricted, _ = login_client(fresh_app, role="restricted", email="rst@example.local")
    r_token = restricted.post("/api/tokens", json={"name": "x"}).json()["token"]
    r_names = {t["name"] for t in _rpc(client, r_token, "tools/list").json()["result"]["tools"]}
    assert "check_calendar" in r_names
    assert "compose_draft" not in r_names
    r = _rpc(client, r_token, "tools/call", {"name": "compose_draft", "arguments": {}})
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
    client, token, uid, _ = token_client
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
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone()

    r = _rpc(client, token, "tools/call",
             {"name": "pending_confirm", "arguments": {"pending_id": pending["pending_id"]}})
    assert r.json()["result"]["isError"] is False, r.text
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
