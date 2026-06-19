# SPDX-License-Identifier: MIT
# Ported from NousResearch/hermes-agent (MIT,
# https://github.com/NousResearch/hermes-agent). Sources:
#   - agent/message_sanitization.py: _sanitize_surrogates,
#     _sanitize_structure_surrogates, _sanitize_messages_surrogates,
#     _escape_invalid_chars_in_json_strings, _repair_tool_call_arguments
#   - run_agent.py: _sanitize_tool_calls_for_strict_api
# Cosmetic edits: module docstring + log names + removed `_` prefix on the
# public names (these are intended for cross-module use in Yorik's agent
# loop, not module-private helpers).
"""Defensive sanitization for LLM-bound messages and tool payloads.

These are the bugs Hermes already paid for: lone UTF-16 surrogates from
Google-Docs/Word clipboard paste that crash json.dumps inside the OpenAI
SDK; malformed JSON tool_call arguments from local llama.cpp backends;
strict-OpenAI providers (Mistral, Fireworks) that 400-reject unknown
fields like `call_id` that Codex Responses API attaches.

Yorik's agent loop calls:
  - sanitize_messages_surrogates(messages) before every API call
  - repair_tool_call_arguments(raw_args, tool_name) on every tool dispatch
  - sanitize_tool_calls_for_strict_api(msg) when we add a strict provider

All functions are stateless. Mutating helpers mutate their input in place
where the docstring says so.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("yorik.agent.sanitize")

# Lone surrogate code points are invalid in UTF-8 and crash json.dumps
# inside the OpenAI SDK.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


# ---------------------------------------------------------------------------
# Surrogate scrubbing
# ---------------------------------------------------------------------------


def sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD.

    Fast no-op when the text contains no surrogates.
    """
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub("�", text)
    return text


def sanitize_structure_surrogates(payload: Any) -> bool:
    """Replace surrogate code points in nested dict/list payloads in-place.

    Returns True if any surrogates were replaced. Used to scrub structured
    fields (e.g. `reasoning_details` — an array of dicts with summary/text
    strings) that flat per-field checks don't reach.
    """
    found = False

    def _walk(node):
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[key] = _SURROGATE_RE.sub("�", value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[idx] = _SURROGATE_RE.sub("�", value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)

    _walk(payload)
    return found


def sanitize_messages_surrogates(messages: list) -> bool:
    """Sanitize surrogate characters from all string content in a messages list.

    Walks message dicts in-place. Returns True if any surrogates were found
    and replaced. Covers content/text, name, tool_call metadata/arguments,
    AND any additional string or nested fields (`reasoning`,
    `reasoning_content`, `reasoning_details`, ...) so retries don't fail
    on a non-content field.
    """
    found = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and _SURROGATE_RE.search(content):
            msg["content"] = _SURROGATE_RE.sub("�", content)
            found = True
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and _SURROGATE_RE.search(text):
                        part["text"] = _SURROGATE_RE.sub("�", text)
                        found = True
        name = msg.get("name")
        if isinstance(name, str) and _SURROGATE_RE.search(name):
            msg["name"] = _SURROGATE_RE.sub("�", name)
            found = True
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                if isinstance(tc_id, str) and _SURROGATE_RE.search(tc_id):
                    tc["id"] = _SURROGATE_RE.sub("�", tc_id)
                    found = True
                fn = tc.get("function")
                if isinstance(fn, dict):
                    fn_name = fn.get("name")
                    if isinstance(fn_name, str) and _SURROGATE_RE.search(fn_name):
                        fn["name"] = _SURROGATE_RE.sub("�", fn_name)
                        found = True
                    fn_args = fn.get("arguments")
                    if isinstance(fn_args, str) and _SURROGATE_RE.search(fn_args):
                        fn["arguments"] = _SURROGATE_RE.sub("�", fn_args)
                        found = True
        # Walk any additional string / nested fields.
        for key, value in msg.items():
            if key in {"content", "name", "tool_calls", "role"}:
                continue
            if isinstance(value, str):
                if _SURROGATE_RE.search(value):
                    msg[key] = _SURROGATE_RE.sub("�", value)
                    found = True
            elif isinstance(value, (dict, list)):
                if sanitize_structure_surrogates(value):
                    found = True
    return found


# ---------------------------------------------------------------------------
# Tool-call argument JSON repair
# ---------------------------------------------------------------------------


def _escape_invalid_chars_in_json_strings(raw: str) -> str:
    """Escape unescaped control chars inside JSON string values.

    Walks the raw JSON char-by-char, tracking whether we are inside a
    double-quoted string. Inside strings, replaces literal control chars
    (0x00-0x1F) with \\uXXXX equivalents. Used as a last-pass repair when
    json.loads(strict=False) is not enough.
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
        i += 1
    return "".join(out)


def repair_tool_call_arguments(raw_args: str, tool_name: str = "?") -> str:
    """Attempt to repair malformed tool_call argument JSON.

    Local models (GLM, llama.cpp variants) can emit truncated JSON,
    trailing commas, literal Python None, unescaped tabs/newlines, etc.
    Strict API endpoints reject these with HTTP 400. This function tries
    several repair passes; if all fail it returns "{}" so the request
    doesn't crash the whole session. All repairs log at WARNING.
    """
    raw_stripped = raw_args.strip() if isinstance(raw_args, str) else ""

    # Fast-path: empty -> empty object
    if not raw_stripped:
        logger.warning("Sanitized empty tool_call arguments for %s", tool_name)
        return "{}"

    # Python-literal None -> normalise to {}
    if raw_stripped == "None":
        logger.warning("Sanitized Python-None tool_call arguments for %s", tool_name)
        return "{}"

    # Pass 0: strict=False parses literal control chars; reserialising
    # produces wire-valid JSON. Most common local-model repair case.
    try:
        parsed = json.loads(raw_stripped, strict=False)
        reserialised = json.dumps(parsed, separators=(",", ":"))
        if reserialised != raw_stripped:
            logger.warning(
                "Repaired unescaped control chars in tool_call arguments for %s",
                tool_name,
            )
        return reserialised
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Pass 1: strip trailing commas; close unclosed structures; trim
    # excess closers.
    fixed = raw_stripped
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    open_curly = fixed.count("{") - fixed.count("}")
    open_bracket = fixed.count("[") - fixed.count("]")
    if open_curly > 0:
        fixed += "}" * open_curly
    if open_bracket > 0:
        fixed += "]" * open_bracket
    for _ in range(50):
        try:
            json.loads(fixed)
            break
        except json.JSONDecodeError:
            if fixed.endswith("}") and fixed.count("}") > fixed.count("{"):
                fixed = fixed[:-1]
            elif fixed.endswith("]") and fixed.count("]") > fixed.count("["):
                fixed = fixed[:-1]
            else:
                break

    try:
        json.loads(fixed)
        logger.warning(
            "Repaired malformed tool_call arguments for %s: %s -> %s",
            tool_name, raw_stripped[:80], fixed[:80],
        )
        return fixed
    except json.JSONDecodeError:
        pass

    # Pass 2: escape unescaped control chars inside JSON strings, retry.
    try:
        escaped = _escape_invalid_chars_in_json_strings(fixed)
        if escaped != fixed:
            json.loads(escaped)
            logger.warning(
                "Repaired control-char-laced tool_call arguments for %s: %s -> %s",
                tool_name, raw_stripped[:80], escaped[:80],
            )
            return escaped
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Last resort: return empty object.
    logger.warning(
        "Unrepairable tool_call arguments for %s — replaced with empty object (was: %s)",
        tool_name, raw_stripped[:80],
    )
    return "{}"


# ---------------------------------------------------------------------------
# Strict-OpenAI-API tool_call stripping
# ---------------------------------------------------------------------------


_STRICT_API_STRIP_KEYS = frozenset({"call_id", "response_item_id"})


def sanitize_tool_calls_for_strict_api(api_msg: dict) -> dict:
    """Strip Codex Responses API fields from tool_calls for strict providers.

    Providers like Mistral and Fireworks validate the Chat Completions
    schema and reject unknown fields (call_id, response_item_id) with
    400/422. These fields are preserved in the caller's message history;
    this method only modifies the outgoing API copy.

    Creates new tool_call dicts rather than mutating in place, so the
    original messages list retains the extra keys for potential fallback
    to a Codex provider.
    """
    tool_calls = api_msg.get("tool_calls")
    if not isinstance(tool_calls, list):
        return api_msg
    api_msg["tool_calls"] = [
        {k: v for k, v in tc.items() if k not in _STRICT_API_STRIP_KEYS}
        if isinstance(tc, dict)
        else tc
        for tc in tool_calls
    ]
    return api_msg


__all__ = [
    "sanitize_surrogates",
    "sanitize_structure_surrogates",
    "sanitize_messages_surrogates",
    "repair_tool_call_arguments",
    "sanitize_tool_calls_for_strict_api",
]
