"""OpenAI-format message helpers — pure functions, no state.

We store and send messages in OpenAI Chat Completions format throughout.
Same dict shape we'd hand to ``client.chat.completions.create``:

    {"role": "system",    "content": "..."}
    {"role": "user",      "content": "..."}
    {"role": "assistant", "content": "..." | None,
                          "tool_calls": [{"id": ..., "type": "function",
                                          "function": {"name": ..., "arguments": "..."}}]}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": "..."}

These helpers cover construction, normalisation of LLM responses into
this shape, and the defensive paths Hermes already taught us:

- ``sanitize_messages_surrogates`` before send (prevents UTF-8 encode
  crashes from clipboard-paste lone surrogates)
- ``repair_tool_call_arguments`` on every tool_call we dispatch (local
  llama.cpp models emit malformed JSON often enough to matter)
- ``sanitize_tool_calls_for_strict_api`` when the active provider is
  one of the strict ones (Mistral, Fireworks) — currently inactive but
  one-line wired
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .sanitize import (
    repair_tool_call_arguments,
    sanitize_messages_surrogates,
    sanitize_surrogates,
)

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def system_message(text: str) -> Dict[str, Any]:
    """System prompt message. Surrogate-scrubbed."""
    return {"role": "system", "content": sanitize_surrogates(text)}


def user_message(text: str, name: Optional[str] = None) -> Dict[str, Any]:
    """User turn. ``name`` is optional and rarely set; primarily useful
    when multiple human users share a conversation thread."""
    msg: Dict[str, Any] = {"role": "user", "content": sanitize_surrogates(text)}
    if name:
        msg["name"] = name
    return msg


def assistant_message(
    content: Optional[str],
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Assistant turn. Either ``content`` (final answer) or ``tool_calls``
    (call out, no answer yet) or both (rare; some models emit thinking
    text before tool_calls). OpenAI accepts content=None when tool_calls
    is present."""
    msg: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def tool_message(tool_call_id: str, name: str, content: Any) -> Dict[str, Any]:
    """Tool result message. ``content`` may be a string (typical) or a
    JSON-serialisable object — we stringify non-strings here so the
    outgoing payload doesn't surprise the API."""
    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            content = str(content)
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": sanitize_surrogates(content),
    }


# ---------------------------------------------------------------------------
# Normalisation of LLM responses
# ---------------------------------------------------------------------------


def normalize_assistant_response(
    choice_message: Any,
) -> Dict[str, Any]:
    """Convert an openai SDK ``chat.completions.create`` choice.message
    (a Pydantic model) into our plain-dict assistant message shape.

    Crucially: tool_call ``arguments`` are repaired via
    :func:`repair_tool_call_arguments` here, ONCE, on the way in. The
    loop and the tool dispatcher then trust the args string is valid
    JSON.

    Handles both shapes the SDK emits:
      - Pydantic models (the normal case): ``choice.message.content``,
        ``.tool_calls[*].id``, ``.tool_calls[*].function.name``, etc.
      - Plain dicts (some test/mock paths): same fields by key.
    """
    content = _attr_or_key(choice_message, "content")
    raw_tool_calls = _attr_or_key(choice_message, "tool_calls") or []

    normalised_calls: List[Dict[str, Any]] = []
    for tc in raw_tool_calls:
        tc_id = _attr_or_key(tc, "id") or ""
        tc_type = _attr_or_key(tc, "type") or "function"
        fn = _attr_or_key(tc, "function") or {}
        fn_name = _attr_or_key(fn, "name") or ""
        raw_args = _attr_or_key(fn, "arguments")
        if raw_args is None:
            repaired_args = "{}"
        else:
            repaired_args = repair_tool_call_arguments(
                raw_args if isinstance(raw_args, str) else json.dumps(raw_args),
                tool_name=fn_name or "?",
            )
        normalised_calls.append({
            "id": tc_id,
            "type": tc_type,
            "function": {"name": fn_name, "arguments": repaired_args},
        })

    return assistant_message(content, normalised_calls or None)


# ---------------------------------------------------------------------------
# Pre-send hygiene
# ---------------------------------------------------------------------------


def prepare_for_send(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Final hygiene pass before handing messages to the API.

    Mutates the list in place (returns the same list for chaining
    convenience). Currently: surrogate scrub. A future provider switch
    would also call :func:`sanitize_tool_calls_for_strict_api` here on
    the assistant messages.
    """
    sanitize_messages_surrogates(messages)
    return messages


# ---------------------------------------------------------------------------
# Convenience for the loop
# ---------------------------------------------------------------------------


def parse_tool_call_args(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a normalised tool_call's ``arguments`` JSON string into a dict.

    ``normalize_assistant_response`` already ran ``repair_tool_call_arguments``,
    so the parse should never fail; this is a thin convenience that returns
    ``{}`` on any residual oddity rather than raising into the loop.
    """
    raw = (tool_call.get("function") or {}).get("arguments", "{}")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def has_tool_calls(message: Dict[str, Any]) -> bool:
    """True iff this assistant message has at least one tool_call."""
    return bool(message.get("tool_calls"))


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _attr_or_key(obj: Any, name: str) -> Any:
    """Read ``name`` from ``obj`` whether it's a dict or a Pydantic model."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


__all__ = [
    "system_message",
    "user_message",
    "assistant_message",
    "tool_message",
    "normalize_assistant_response",
    "prepare_for_send",
    "parse_tool_call_args",
    "has_tool_calls",
]
