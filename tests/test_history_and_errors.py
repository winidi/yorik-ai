"""Phase 3 contracts that need no database: history trimming keeps the
model's input inside the context window, and LLM failures reach the
user as one readable sentence in their language."""

from __future__ import annotations

import pytest


def _turns(n: int, size: int = 100):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"u{i} " + "x" * size})
        out.append({"role": "assistant", "content": f"a{i} " + "y" * size})
    return out


def test_trim_history_noop_when_it_fits():
    from backend.agent.conversation_io import trim_history
    msgs = _turns(3)
    assert trim_history(msgs, max_chars=10_000) == msgs


def test_trim_history_drops_oldest_first():
    from backend.agent.conversation_io import trim_history
    msgs = _turns(10, size=100)
    kept = trim_history(msgs, max_chars=650)
    assert kept
    assert kept[-1] == msgs[-1]
    assert sum(len(m["content"]) for m in kept) <= 650
    assert kept[0]["content"].startswith("u")


def test_trim_history_never_starts_on_a_tool_result():
    from backend.agent.conversation_io import trim_history
    msgs = [
        {"role": "user", "content": "old " * 50},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                                "function": {"name": "invoke_skill", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result " * 40},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "answer"},
    ]
    kept = trim_history(msgs, max_chars=120)
    assert kept[0]["role"] in ("user", "assistant")
    assert not (kept[0]["role"] == "assistant" and kept[0].get("tool_calls"))
    assert kept[0]["role"] != "tool"
    assert kept[-2:] == msgs[-2:]


def test_context_budget_follows_env(monkeypatch):
    from backend.agent.conversation_io import context_chars_budget
    monkeypatch.setenv("YORIK_LLM_CTX", "16384")
    small = context_chars_budget()
    monkeypatch.setenv("YORIK_LLM_CTX", "65536")
    big = context_chars_budget()
    assert 8_000 <= small < big


class _Llm:
    base_url = "http://127.0.0.1:8080/v1"
    model = "qwen3.5-9b"


@pytest.mark.parametrize("exc,lang,needle", [
    (type("APIConnectionError", (Exception,), {})("Connection refused"), "de", "erreiche das Sprachmodell"),
    (type("APITimeoutError", (Exception,), {})("Request timed out"), "en", "didn't answer in time"),
    (type("BadRequestError", (Exception,), {})("the request exceeds the available context size"), "de", "zu lang"),
    (RuntimeError("something odd"), "en", "returned an error"),
])
def test_friendly_llm_error(exc, lang, needle):
    from backend.agent.loop import friendly_llm_error
    text = friendly_llm_error(exc, language=lang, llm=_Llm())
    assert needle in text
    assert "Traceback" not in text
