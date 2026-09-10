"""ask_agent — Yorik delegating a question to the household's strong agent."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest


def _ctx(user_id="u1", conversation_id="c1"):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role="member", user_id=user_id, conversation_id=conversation_id)


@pytest.fixture
def agent(monkeypatch):
    """Patch httpx.AsyncClient so the skill talks to an in-memory agent."""
    from backend.skills.ask_agent import skill as S
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        if seen.get("mode") == "401":
            return httpx.Response(401, json={"error": "bad key"})
        if seen.get("mode") == "down":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"choices": [{"message": {"content": "  Antwort vom Agenten.  "}}]})

    real = httpx.AsyncClient

    def factory(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(S, "_user", lambda ctx: ("Anna", "de"))
    monkeypatch.setenv("HOMEOS_AGENT_URL", "http://agent.test:8642/v1/")
    monkeypatch.setenv("HOMEOS_AGENT_KEY", "secret")
    monkeypatch.setenv("HOMEOS_AGENT_NAME", "Hermes")
    return seen


def test_delegates_with_session_and_key(agent):
    from backend.skills.ask_agent.skill import execute
    out = asyncio.run(execute(_ctx(), question="Was steht oben in meiner Aufgabenliste?", context="wir reden über Video 9"))
    assert out["answer"] == "Antwort vom Agenten." and out["agent"] == "Hermes"
    assert agent["url"] == "http://agent.test:8642/v1/chat/completions"
    assert agent["headers"]["authorization"] == "Bearer secret"
    assert agent["headers"]["x-hermes-session-id"] == "yorik-u1-c1"
    msgs = agent["body"]["messages"]
    assert msgs[0]["role"] == "system" and "Anna" in msgs[0]["content"] and "(de)" in msgs[0]["content"]
    assert msgs[1]["content"].startswith("Was steht oben") and "Context: wir reden" in msgs[1]["content"]
    # Chat-sized questions run without thinking unless config.env says otherwise.
    assert agent["body"]["model_options"] == {"reasoning_effort": "none"}


def test_reasoning_level_from_config(agent, monkeypatch):
    from backend.skills.ask_agent.skill import execute
    monkeypatch.setenv("HOMEOS_AGENT_REASONING", "medium")
    asyncio.run(execute(_ctx(), question="Denk gründlich nach."))
    assert agent["body"]["model_options"] == {"reasoning_effort": "medium"}


def test_errors_are_reported_not_invented(agent, monkeypatch):
    from backend.skills.ask_agent.skill import execute
    agent["mode"] = "401"
    out = asyncio.run(execute(_ctx(), question="x"))
    assert "error" in out and "API key" in out["error"] and "answer" not in out
    agent["mode"] = "down"
    out = asyncio.run(execute(_ctx(), question="x"))
    assert "not reachable" in out["error"]
    monkeypatch.setenv("HOMEOS_AGENT_URL", "")
    out = asyncio.run(execute(_ctx(), question="x"))
    assert out["error"] == "no agent configured"


def test_not_exposed_over_mcp(fresh_app):
    from backend.mcp_server import list_tools, _may_call
    from backend.skills import get_registry
    user = {"id": "u", "role": "admin"}
    assert "ask_agent" not in {t["name"] for t in list_tools(user)}
    assert _may_call(user, get_registry().get("ask_agent")) is False
