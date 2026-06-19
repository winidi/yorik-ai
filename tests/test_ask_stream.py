"""SSE /api/ask/stream contract test.

Pins the wire format the React chat consumes:

  data: {"phase": "iter_start",       ...}\n\n
  data: {"phase": "text_delta",       "text": "..."}\n\n  ← token streaming
  data: {"phase": "tool_call_start",  ...}\n\n  ← LLM constructing the call
  data: {"phase": "tool_start",       ...}\n\n  ← args ready, dispatching
  data: {"phase": "tool_done",        ...}\n\n
  data: {"phase": "final",            ...}\n\n   ← always last on success
  data: {"phase": "error",            ...}\n\n   ← when the loop raises

We stub `ask.ask_async_stream` (async generator) to yield typed events
in a known order so we don't need a live LLM. The point isn't to test
the agent loop — it's to test that whatever the loop yields hits the
wire as SSE in the right shape.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient


def _logged_in_client(app):
    """Create a real user + session and return a TestClient with the
    session cookie already attached. The /api/* middleware (defined in
    backend/main.py:require_session_for_api) checks the cookie before
    any per-route dependency runs, so dependency_overrides alone are
    not enough."""
    from backend import auth_sessions
    from backend.database import DEFAULT_DB_PATH, conn_ctx
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO user_profiles "
            "(name, email, role, voice_id, password_hash, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Test User", "t@example.local", "admin", "vid",
             auth_sessions.hash_password("pytestpw123"), "en"),
        )
        uid = cur.lastrowid
        conn.commit()
    sid = auth_sessions.create_session(uid, user_agent="pytest", ip="127.0.0.1")
    client = TestClient(app)
    client.cookies.set(auth_sessions.COOKIE_NAME, sid)
    return client


def _parse_sse(body: str) -> list[dict]:
    """Tiny SSE parser — splits on blank lines and JSON-decodes the
    data: payload of each frame. Good enough for the contract tests."""
    frames = []
    for chunk in body.split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: "):]))
                break
    return frames


def test_stream_emits_progress_then_final(fresh_app, monkeypatch):
    """Verify the SSE wire format for a realistic event sequence:
    iter_start → text_delta(×N) → tool_call_start → tool_start → tool_done
    → text_delta → final.
    """
    from backend.agent import streaming as _stream

    async def fake_ask_async_stream(message, **kwargs):
        # Yield the same typed events the in-tree loop yields, in a
        # known order. The endpoint's job is to map each to the right
        # SSE phase + JSON shape.
        yield _stream.IterationStart(n=1)
        yield _stream.TextDelta(text="Lass mich nachsehen — ")
        yield _stream.ToolCallStart(id="call_1", name="web_search")
        yield _stream.ToolCallReady(id="call_1", name="web_search",
                                     arguments={"q": "wetter"})
        await asyncio.sleep(0)
        yield _stream.ToolResultEvent(id="call_1", name="web_search",
                                       result_for_llm="ok",
                                       ui_actions=[])
        yield _stream.IterationStart(n=2)
        yield _stream.TextDelta(text="Sonnig.")
        yield _stream.FinalResult(response={
            "response":        "Lass mich nachsehen — Sonnig.",
            "sql_used":        None,
            "ui_actions":      [],
            "conversation_id": "conv-1",
            "agent_trace":     {"iterations": 2},
        })

    from backend import ask, main as backend_main
    monkeypatch.setattr(ask, "ask_async_stream", fake_ask_async_stream)
    # Pretend LLM is reachable so the offline branch doesn't shortcut us.
    monkeypatch.setattr(backend_main, "_llm_reachable", lambda: True)

    client = _logged_in_client(fresh_app)
    resp = client.post("/api/ask/stream",
                       json={"message": "wie wird das wetter?"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Sanity on the anti-buffering headers our docs promise nginx admins.
    assert resp.headers.get("x-accel-buffering") == "no"
    assert resp.headers.get("cache-control") == "no-cache"

    frames = _parse_sse(resp.text)
    phases = [f["phase"] for f in frames]
    assert phases == [
        "iter_start",       # IterationStart(1)
        "text_delta",       # TextDelta("Lass mich nachsehen — ")
        "tool_call_start",  # ToolCallStart
        "tool_start",       # ToolCallReady (args ready)
        "tool_done",        # ToolResultEvent
        "iter_start",       # IterationStart(2)
        "text_delta",       # TextDelta("Sonnig.")
        "final",            # FinalResult
    ]
    # Text deltas carry their incremental text.
    deltas = [f["text"] for f in frames if f["phase"] == "text_delta"]
    assert deltas == ["Lass mich nachsehen — ", "Sonnig."]
    # Final carries the full response dict.
    final = frames[-1]
    assert final["response"]        == "Lass mich nachsehen — Sonnig."
    assert final["conversation_id"] == "conv-1"
    assert final["agent_trace"]["iterations"] == 2


def test_stream_emits_error_phase_when_agent_raises(fresh_app, monkeypatch):
    async def boom_stream(*args, **kwargs):
        # Raise mid-stream — the endpoint's try/except wrapper should
        # convert it to a {"phase":"error"} frame as the LAST SSE event.
        raise RuntimeError("simulated tool crash")
        # Unreachable but required so Python treats this as an async generator.
        yield  # pragma: no cover

    from backend import ask, main as backend_main
    monkeypatch.setattr(ask, "ask_async_stream", boom_stream)
    monkeypatch.setattr(backend_main, "_llm_reachable", lambda: True)

    client = _logged_in_client(fresh_app)
    resp = client.post("/api/ask/stream", json={"message": "hi"})
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    assert frames, "expected at least one SSE frame on error"
    assert frames[-1]["phase"] == "error"
    assert "simulated tool crash" in frames[-1]["error"]
    assert "RuntimeError" in frames[-1]["error"]


def test_stream_offline_branch_returns_single_final(fresh_app, monkeypatch):
    """When the LLM is unreachable, the endpoint must short-circuit
    with one `final` frame so the React consumer doesn't have to
    special-case offline mode."""
    from backend import main as backend_main
    monkeypatch.setattr(backend_main, "_llm_reachable", lambda: False)

    client = _logged_in_client(fresh_app)
    resp = client.post("/api/ask/stream", json={"message": "anything"})
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    assert len(frames) == 1
    assert frames[0]["phase"] == "final"
    assert frames[0]["degraded"] is True
    assert "can't reach the language model" in frames[0]["response"]


def test_stream_requires_auth(fresh_app):
    """Without the dependency override, the endpoint must 401 the same
    way every other protected /api/ endpoint does."""
    client = TestClient(fresh_app)
    resp = client.post("/api/ask/stream", json={"message": "hi"})
    assert resp.status_code == 401
