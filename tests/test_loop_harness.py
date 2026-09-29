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
    # The wrap-up call got another tool call back (no text) → the
    # approved fallback sentence, not the old "Schritte ausgegangen".
    assert out["response"].startswith("Dazu habe ich nichts Sicheres gefunden")
    assert len(tool.seen) <= 3


def test_budget_end_answers_with_what_was_found(admin_id):
    """Chat test 2026-09-26: the step budget ended on a technical note;
    now one tool-less call turns the findings into an answer."""
    tool = _EchoTool()
    replies = [
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("echo_tool", '{"text": "again"}', f"c{i}")]}
        for i in range(3)
    ] + [{"role": "assistant", "content": "Ich habe nur 'again' gefunden, mehr nicht."}]
    fake = _FakeLlm(replies)
    out = _run(fake, tool, language="de", max_iterations=3, user_id=admin_id)
    assert out["response"] == "Ich habe nur 'again' gefunden, mehr nicht."
    last_prompt = fake.calls[-1]
    assert last_prompt[-1]["content"].startswith("No more tool calls this turn.")


def test_llm_failure_is_one_readable_sentence(admin_id):
    """ask.py wraps loop failures with error_response; the sentence must be
    the user's language and never a traceback."""
    from backend.agent import loop
    exc = type("APIConnectionError", (Exception,), {})("Connection refused")
    out = loop.error_response(exc, conversation_id="x", llm=_FakeLlm([]), language="de")
    assert out["error"] is True
    assert "erreiche das Sprachmodell" in out["response"]
    assert "Traceback" not in out["response"] and "APIConnectionError" not in out["response"]


def test_a_long_conversation_keeps_its_beginning_on_disk(admin_id, monkeypatch):
    """The model sees only the recent turns; until 2026-09-29 the trimmed
    list was also what got saved, so the oldest turns vanished for good."""
    from backend.agent import conversation_io as ci, loop
    from backend.agent.context import User
    from backend.agent.tools import ToolRegistry
    monkeypatch.setenv("YORIK_LLM_CTX", "1000")                 # budget floor: 8,000 chars
    old = []
    for i in range(40):
        old += [{"role": "user", "content": f"u{i} " + "x" * 200},
                {"role": "assistant", "content": f"a{i} " + "y" * 200}]
    ci.save_messages("conv-long", "admin", admin_id, old)
    fake = _FakeLlm([{"role": "assistant", "content": "ok"}])
    reg = ToolRegistry(); reg.register(_EchoTool())
    asyncio.run(loop.ask("hallo", user=User(id=admin_id, role="admin", language="en", name=None),
                         registry=reg, llm=fake, system_prompt="You are a test butler.",
                         conversation_id="conv-long"))
    seen = " ".join(str(m.get("content")) for m in fake.calls[0])
    assert "u0 " not in seen and "u39 " in seen                   # the model got the recent part
    saved = ci.load_messages("conv-long", admin_id)
    assert saved[0]["content"].startswith("u0 ") and len(saved) == len(old) + 2
    assert saved[-1]["content"] == "ok"


class _SlowStreamLlm:
    """Streams 50 words slowly; notes how many it gave before it was closed."""
    base_url = "http://fake-llm.local/v1"
    model = "fake-9b"

    def __init__(self):
        self.sent, self.closed = 0, False

    def chat_stream(self, messages, tools=None, **_kw):
        import time
        from types import SimpleNamespace as NS
        try:
            for i in range(50):
                time.sleep(0.02)
                self.sent += 1
                yield NS(choices=[NS(delta=NS(content=f"w{i} ", tool_calls=None), finish_reason=None)])
            yield NS(choices=[NS(delta=None, finish_reason="stop")])
        finally:
            self.closed = True

    def chat(self, messages, tools=None, **_kw):
        return {"role": "assistant", "content": "", "_usage": None, "_finish_reason": "stop"}


def test_a_person_who_leaves_stops_the_model_and_the_turn_is_not_saved(admin_id):
    import threading, time
    from backend.agent import conversation_io as ci, loop, streaming
    from backend.agent.context import User
    from backend.agent.tools import ToolRegistry
    fake, left = _SlowStreamLlm(), threading.Event()

    async def run():
        events = []
        async for ev in loop.ask_stream("hallo", user=User(id=admin_id, role="admin", language="de"),
                                        registry=ToolRegistry(), llm=fake, system_prompt="test",
                                        conversation_id="conv-left", cancel=left):
            events.append(ev)
            if isinstance(ev, streaming.TextDelta):
                left.set()                                   # the browser went away
        return events

    events = asyncio.run(run())
    time.sleep(0.2)                                          # the pump thread notices
    assert fake.closed and fake.sent < 50                    # the model stopped early
    assert not any(isinstance(e, streaming.FinalResult) for e in events)
    assert ci.load_messages("conv-left", admin_id) == []     # nothing saved
