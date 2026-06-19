# SPDX-License-Identifier: MIT
# Ported verbatim from NousResearch/hermes-agent agent/prompt_caching.py
# (MIT, https://github.com/NousResearch/hermes-agent). Cosmetic edits to
# the module docstring to call out that Yorik doesn't activate it yet.
"""Anthropic prompt caching strategy — system_and_3 layout.

Places up to 4 `cache_control` breakpoints on the outgoing message list:
the system prompt plus the last 3 non-system messages. All at the same
TTL (5m by default; 1h supported). Cuts input-token cost ~75% on
multi-turn conversations against Anthropic endpoints.

**Yorik status: shipped but inactive.** Our default LLM is Qwen via
llama-swap (OpenAI-compat), which has no concept of Anthropic
cache_control markers. This module is in place so the day we add a
Claude provider in `agent/llm.py`, we just call
`apply_anthropic_cache_control(messages)` on the outgoing payload — no
new code, no surprise debugging at 2am.

Pure functions — no class state, no AIAgent dependency.
"""

import copy
from typing import Any, Dict, List


def _apply_cache_marker(msg: dict, cache_marker: dict, native_anthropic: bool = False) -> None:
    """Add cache_control to a single message, handling all format variations."""
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "tool":
        if native_anthropic:
            msg["cache_control"] = cache_marker
        return

    if content is None or content == "":
        msg["cache_control"] = cache_marker
        return

    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": cache_marker}
        ]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = cache_marker


def _build_marker(ttl: str) -> Dict[str, str]:
    """Build a cache_control marker dict for the given TTL ('5m' or '1h')."""
    marker: Dict[str, str] = {"type": "ephemeral"}
    if ttl == "1h":
        marker["ttl"] = "1h"
    return marker


def apply_anthropic_cache_control(
    api_messages: List[Dict[str, Any]],
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
) -> List[Dict[str, Any]]:
    """Apply system_and_3 caching strategy to messages for Anthropic models.

    Places up to 4 cache_control breakpoints: system prompt + last 3
    non-system messages, all at the same TTL.

    Returns:
        Deep copy of messages with cache_control breakpoints injected.
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker = _build_marker(cache_ttl)

    breakpoints_used = 0

    if messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
        breakpoints_used += 1

    remaining = 4 - breakpoints_used
    non_sys = [i for i in range(len(messages)) if messages[i].get("role") != "system"]
    for idx in non_sys[-remaining:]:
        _apply_cache_marker(messages[idx], marker, native_anthropic=native_anthropic)

    return messages


__all__ = ["apply_anthropic_cache_control"]
