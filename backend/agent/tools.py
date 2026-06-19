"""Tool protocol + registry + JSON-schema derivation.

A Tool in our world is anything the LLM can call via OpenAI tool-call
syntax. Minimum surface:

    class MyTool:
        name = "my_tool"
        description = "Does X."
        json_schema = {...}             # OpenAI ``parameters`` object
        async def execute(self, ctx, args) -> ToolResult: ...

It's a runtime Protocol — duck-typed, no inheritance required. Each
tool's ``execute`` returns a :class:`ToolResult` dict with ``result_for_llm``
(the text the model sees in the next turn) plus optional ``ui_actions``
(forwarded to the React frontend) and ``metadata`` (for audit/telemetry).

The :class:`ToolRegistry` is a thin dict-of-tools with a single dispatch
entry point + a ``.schemas()`` builder that produces the OpenAI ``tools``
array we pass to ``chat.completions.create``.

JSON-schema derivation: ``schema_from_skill_inputs`` takes the
``inputs:`` map from a skill's SKILL.md frontmatter (Yorik convention —
typed at the manifest level) and produces a valid OpenAI ``parameters``
object. This is what lets ``use_skill(name="X", args={...})`` validate
correctly against the model's tool-call output without any per-skill
schema code.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, runtime_checkable

from .context import ToolContext

log = logging.getLogger("yorik.agent.tools")


# ---------------------------------------------------------------------------
# Tool result shape
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """What a tool returns to the loop.

    ``result_for_llm`` is the only thing the LLM sees in the next turn —
    keep it short and structured (JSON-ish text is fine, but enormous
    blobs eat the context window). ``ui_actions`` are forwarded to the
    React frontend via the response dict; ``metadata`` is for our own
    audit/telemetry.

    We do NOT carry a ``success`` flag — the BLOCKED-vs-success bug from
    the Vanna era taught us that letting the LLM see a structured error
    string is far more reliable than letting some sentinel bool flip a
    code path. If a tool fails, return that fact in ``result_for_llm``.
    """
    result_for_llm: str
    ui_actions: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Tool(Protocol):
    """Duck-typed Tool protocol — no inheritance required.

    Implementations just need these four attributes/methods. ``execute``
    may be async OR sync; the dispatcher detects via
    :func:`inspect.iscoroutinefunction` and awaits as needed.
    """
    name: str
    description: str
    json_schema: Dict[str, Any]

    async def execute(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult: ...


# ---------------------------------------------------------------------------
# JSON-schema derivation from skill manifests
# ---------------------------------------------------------------------------

# Map skill-manifest type names → JSON Schema type names. Skill manifests
# use the friendly Python-ish names (integer/string/boolean/object/array);
# JSON Schema wants the wire names (integer/string/boolean/object/array
# happen to match, but float/dict/list don't).
_TYPE_ALIASES = {
    "int": "integer", "integer": "integer",
    "float": "number", "number": "number",
    "str": "string", "string": "string",
    "bool": "boolean", "boolean": "boolean",
    "dict": "object", "object": "object",
    "list": "array", "array": "array",
}


def schema_from_skill_inputs(
    inputs: Dict[str, Dict[str, Any]],
    extra_description: str = "",
) -> Dict[str, Any]:
    """Build an OpenAI tool ``parameters`` object from a skill's inputs map.

    Skill manifest convention (Yorik's frontmatter shape, already in
    every backend/skills/*/SKILL.md)::

        inputs:
          event_id:
            type: integer
            required: true
            description: ...
          title:
            type: string
            required: false

    Output: a JSON Schema object the OpenAI SDK accepts::

        {"type": "object",
         "properties": {"event_id": {"type": "integer", "description": "..."},
                        "title":    {"type": "string",  "description": "..."}},
         "required": ["event_id"]}

    Unknown types fall through as ``"string"`` (defensive — better than
    rejecting a manifest with a typo).
    """
    properties: Dict[str, Dict[str, Any]] = {}
    required: List[str] = []
    inputs = inputs or {}
    for arg_name, spec in inputs.items():
        if not isinstance(spec, dict):
            continue
        json_type = _TYPE_ALIASES.get(str(spec.get("type", "string")).lower(), "string")
        prop: Dict[str, Any] = {"type": json_type}
        if spec.get("description"):
            prop["description"] = str(spec["description"])
        if "enum" in spec and isinstance(spec["enum"], list):
            prop["enum"] = spec["enum"]
        properties[arg_name] = prop
        if spec.get("required") is True:
            required.append(arg_name)
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    if extra_description:
        schema["description"] = extra_description
    return schema


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Holds the set of tools the agent can call this turn.

    Not thread-safe — the loop is single-threaded per request. If we
    ever do concurrent tool dispatch (Hermes does; we deferred to a
    later phase), revisit.
    """

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool. Re-registration overwrites silently — useful
        for hot-reload scenarios in tests/dev."""
        if not getattr(tool, "name", None):
            raise ValueError("Tool must have a non-empty .name")
        if not isinstance(tool.name, str):
            raise TypeError(f"Tool.name must be str, got {type(tool.name).__name__}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def schemas(self, names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Return the OpenAI ``tools`` array for this turn.

        If ``names`` is given, restrict to that subset (lets a per-role
        ACL be applied per-request). Otherwise return all registered tools.
        """
        tools = (
            [self._tools[n] for n in names if n in self._tools]
            if names is not None
            else self.all()
        )
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.json_schema or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]

    async def dispatch(
        self, name: str, args: Dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Execute a tool by name. Awaits async tools, calls sync tools directly.

        On unknown tool: returns a ToolResult with a clear LLM-facing error
        instead of raising — same philosophy as the BLOCKED-marker work,
        the LLM recovers in-turn from explanatory errors better than from
        exceptions percolating up to the loop.
        """
        tool = self._tools.get(name)
        if tool is None:
            # Cloud-LLM safety net: some hosted Qwen 3.5 9B serving stacks
            # (verified on OpenRouter→SiliconFlow) emit a skill name as
            # a top-level tool call (e.g. `search_documents(query="x")`)
            # instead of the meta-tool form `invoke_skill(name="search_
            # documents", args={"query": "x"})` that local llama.cpp
            # serving Qwen reliably produces. The skill index in the
            # system prompt lists 64 skill names that look structurally
            # like tools, and a weak-instruction cloud-quantized model
            # occasionally collapses the "call them via invoke_skill"
            # meta-instruction. When that happens, transparently route
            # the call through invoke_skill so the user gets a working
            # chat instead of a "tool not registered" rejection.
            #
            # Three safety properties:
            #   1. Local model never enters this branch in observed
            #      regression traces (it always emits invoke_skill
            #      directly, so tool is NOT None above and the existing
            #      dispatch path runs unchanged). The INFO log makes any
            #      unexpected local firing visible immediately — that's
            #      the signal that the local model's behaviour has
            #      drifted toward the cloud failure mode.
            #   2. Names that are neither a registered tool NOR a
            #      registered skill (typos, hallucinated names) still
            #      hit the original "tool not registered" error path
            #      below. Same behaviour as today.
            #   3. The auto-routed call bypasses the loop's
            #      skill_view-first enforcement at loop.py:366-369 /
            #      1025-1028 — that enforcement only fires when
            #      `name == "invoke_skill"`. Skills with strict Pydantic
            #      schemas catch wrong args at the skill boundary;
            #      permissive skills may produce slightly degraded
            #      results when the model guesses args without reading
            #      the manifest. The trade-off favors functioning over
            #      manifest-strictness for cloud users.
            try:
                from backend.skills import get_registry as _get_skills_registry
                if _get_skills_registry().get(name) is not None:
                    invoke_tool = self._tools.get("invoke_skill")
                    if invoke_tool is not None:
                        log.info(
                            "agent.auto_route: %r called as top-level tool, "
                            "routing through invoke_skill", name,
                        )
                        return await invoke_tool.execute(
                            ctx, {"name": name, "args": args or {}}
                        )
            except Exception as _route_exc:  # noqa: BLE001
                # Any failure in the auto-route lookup degrades to the
                # original error path — never raise out of dispatch.
                log.debug(
                    "agent.auto_route: lookup failed for %r: %s",
                    name, _route_exc,
                )

            available = ", ".join(self.names()) or "(none registered)"
            # Wording note: original phrasing opened with "ERROR:" and
            # ended "Pick one of those and retry." Same logic as the
            # REJECTED → Read skill_view wording fix from earlier today:
            # words like ERROR / FAIL / Pick-and-retry read as terminal
            # "task impossible" cues to cloud-quantized Qwen and trigger
            # the silent-stop failure mode. Softened to neutral
            # information-request phrasing — the unknown-tool path now
            # only fires for genuinely unrecognised names (the
            # auto-route branch above catches the common
            # skill-called-as-tool case before we reach this), so the
            # message can lead with the most useful hint: try
            # invoke_skill if it might be a skill.
            return ToolResult(
                result_for_llm=(
                    f"Tool {name!r} isn't directly registered. If it's "
                    f"a Yorik skill, call it via "
                    f"invoke_skill(name={name!r}, args={{...}}) — that's "
                    f"the canonical path for skills. Otherwise pick "
                    f"from: {available}."
                ),
                metadata={"unknown_tool": name},
            )
        exec_fn = tool.execute
        try:
            if inspect.iscoroutinefunction(exec_fn):
                result = await exec_fn(ctx, args)
            else:
                result = exec_fn(ctx, args)
                if inspect.isawaitable(result):  # sync def that returned a coro
                    result = await result
        except Exception as exc:  # noqa: BLE001
            # Surface the exception text to the LLM. The loop's guardrails
            # (Phase 2) will catch repeated identical failures.
            return ToolResult(
                result_for_llm=(
                    f"ERROR: tool {name!r} raised "
                    f"{type(exc).__name__}: {exc}. "
                    f"Read the error, adjust your arguments, and retry — "
                    f"or pick a different approach."
                ),
                metadata={"exception": type(exc).__name__, "message": str(exc)},
            )
        # Defensive: tools that return a bare string get auto-wrapped, so
        # quick-and-dirty tools (a one-liner that returns "ok") work too.
        if isinstance(result, str):
            return ToolResult(result_for_llm=result)
        if isinstance(result, dict) and "result_for_llm" in result:
            # Allow plain-dict returns from very small tools
            return ToolResult(
                result_for_llm=str(result["result_for_llm"]),
                ui_actions=result.get("ui_actions") or [],
                metadata=result.get("metadata") or {},
            )
        if not isinstance(result, ToolResult):
            return ToolResult(
                result_for_llm=str(result),
                metadata={"coerced_from": type(result).__name__},
            )
        return result


__all__ = [
    "Tool",
    "ToolResult",
    "ToolRegistry",
    "schema_from_skill_inputs",
]
