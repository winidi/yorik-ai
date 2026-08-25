"""The agent loop against a scripted (fake) LLM.

No model, no network: a stub `.chat()` returns whatever the test
scripts, so the loop's own behaviour is pinned — a tool that raises, a
tool call with broken JSON, a model that never stops calling tools —
without a running llama.cpp.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from tests.conftest import seed_user


class _FakeLlm:
    base_url = "http://fake-llm.local/v1"
    model = "fake-9b"

    def __init__(self, replies: List[Dict[str, Any]]):
        self._replies = list(replies)
        self.calls: List[List[Dict[str, Any]]] = []

    def chat(self, messages, tools=None, **_kw):
        self.calls.append(list(messages))
        if not self._replies:
            return {"role": "assistant", "content": "(fallback) done", "_usage": None, "_finish_reason": "stop"}
        r = self._replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return dict(r, _usage=None, _finish_reason="tool_calls" if r.get("tool_calls") else "stop")


def _tool_call(name: str, arguments: str, cid: str = "call_1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": arguments}}


class _EchoTool:
    name = "echo_tool"
    description = "echoes"
    json_schema = {"type": "object", "properties": {"text": {"type": "string"}}}

    def __init__(self):
        self.seen: List[Dict[str, Any]] = []

    async def execute(self, ctx, args):
        from backend.agent.tools import ToolResult
        self.seen.append(dict(args))
        if args.get("text") == "boom":
            raise RuntimeError("tool exploded")
        return ToolResult(success=True, result_for_llm=f"echo: {args.get('text')}")


def _run(fake, tool, *, language="en", max_iterations=None, user_id=None):
    from backend.agent import loop
    from backend.agent.context import User
    from backend.agent.tools import ToolRegistry
    reg = ToolRegistry()
    reg.register(tool)
    user = User(id=user_id, role="admin", language=language, name=None)
    return asyncio.run(loop.ask(
        "hallo", user=user, registry=reg, llm=fake,
        system_prompt="You are a test butler.", max_iterations=max_iterations,
    ))


@pytest.fixture
def admin_id(fresh_app):
    return seed_user(name="Harness Admin", role="admin", email="harness@example.com")


def test_tool_exception_becomes_a_tool_result_not_a_crash(admin_id):
    tool = _EchoTool()
    fake = _FakeLlm([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("echo_tool", '{"text": "boom"}')]},
        {"role": "assistant", "content": "I recovered."},
    ])
    out = _run(fake, tool, user_id=admin_id)
    assert out["response"] == "I recovered."
    # The second LLM call must have seen a tool message describing the failure.
    tool_msgs = [m for m in fake.calls[1] if m.get("role") == "tool"]
    assert tool_msgs and "exploded" in (tool_msgs[-1].get("content") or "")


def test_broken_tool_arguments_do_not_crash(admin_id):
    tool = _EchoTool()
    fake = _FakeLlm([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("echo_tool", '{"text": "hi",')]},
        {"role": "assistant", "content": "ok"},
    ])
    out = _run(fake, tool, user_id=admin_id)
    assert isinstance(out.get("response"), str)
    assert out.get("error") is not True


def test_never_ending_tool_calls_hit_the_budget_in_the_users_language(admin_id):
    tool = _EchoTool()
    fake = _FakeLlm([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("echo_tool", '{"text": "again"}', f"c{i}")]}
        for i in range(10)
    ])
    out = _run(fake, tool, language="de", max_iterations=3, user_id=admin_id)
    assert "Schritte" in out["response"]
    assert len(tool.seen) <= 3


def test_llm_failure_is_one_readable_sentence(admin_id):
    """ask.py wraps loop failures with error_response; the sentence must be
    the user's language and never a traceback."""
    from backend.agent import loop
    exc = type("APIConnectionError", (Exception,), {})("Connection refused")
    out = loop.error_response(exc, conversation_id="x", llm=_FakeLlm([]), language="de")
    assert out["error"] is True
    assert "erreiche das Sprachmodell" in out["response"]
    assert "Traceback" not in out["response"] and "APIConnectionError" not in out["response"]
