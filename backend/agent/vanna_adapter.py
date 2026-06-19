"""Bridge: wrap existing Vanna-shaped tools as new-protocol Tools.

The legacy ``backend/vanna_agent.py`` + ``backend/ui_tools.py`` define
11 tools (run_sql, use_skill, show_calendar, search_documents, ...)
that inherit ``vanna.core.tool.Tool[ArgsModel]``. Re-implementing each
of them in the new protocol on day one would be a week of porting work
with high regression risk.

Instead this adapter wraps any Vanna Tool instance and exposes it as a
new-protocol :class:`backend.agent.tools.Tool` — same name, same
description, JSON schema derived from the Pydantic args model, and an
``execute`` that:

1. Drains the ``ui_tools._pending_ui_actions`` ContextVar before the
   call (so this turn's actions don't leak into the next).
2. Validates the args dict against the Vanna tool's Pydantic args model.
3. Builds a minimal Vanna ToolContext from our ToolContext.
4. Awaits the Vanna tool's execute.
5. Reads back any side-channel ui_actions and packs them into our
   :class:`ToolResult` (alongside the ``result_for_llm`` and metadata).

Phase 4 of the masterplan ports the 4 critical tools (run_sql /
use_skill / show_calendar / search_documents) natively into the agent
package and removes the Vanna dependency. The other 7 tools (list_apps,
open_app, list_connectors, ...) can stay adapter-wrapped indefinitely
or be ported on demand.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from .context import ToolContext as AgentToolContext
from .tools import ToolResult as AgentToolResult

logger = logging.getLogger("yorik.agent.vanna_adapter")


class VannaToolAdapter:
    """Wrap a Vanna Tool instance so the new agent loop can call it.

    Construct once at app startup, register into the new ToolRegistry,
    forget about it. Stateless apart from holding a reference to the
    underlying Vanna tool.
    """

    def __init__(self, vanna_tool: Any) -> None:
        self._tool = vanna_tool
        self._cached_schema: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Tool protocol attributes
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return str(self._tool.name)

    @property
    def description(self) -> str:
        return str(self._tool.description)

    @property
    def json_schema(self) -> Dict[str, Any]:
        if self._cached_schema is not None:
            return self._cached_schema
        try:
            args_cls = self._tool.get_args_schema()
            # Pydantic v2: model_json_schema() returns a full JSON Schema
            # object with $defs etc. — strip down to the OpenAI-tools-API
            # shape ({type:object, properties, required}).
            raw = args_cls.model_json_schema()
            schema = {
                "type": "object",
                "properties": raw.get("properties") or {},
            }
            if "required" in raw:
                schema["required"] = raw["required"]
            # OpenAI doesn't like the $defs root for tool parameters; drop it.
            # Inlined refs survive in `properties` as their resolved form
            # for the tools we ship, so dropping $defs is safe.
            self._cached_schema = schema
            return schema
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Could not derive JSON schema for Vanna tool %r: %s — "
                "shipping bare object schema",
                self.name, exc,
            )
            self._cached_schema = {"type": "object", "properties": {}}
            return self._cached_schema

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute(self, ctx: AgentToolContext, args: Dict[str, Any]) -> AgentToolResult:
        # Reset the ui_actions buffer so we capture only what THIS tool
        # call emits — leftovers from a previous turn shouldn't leak.
        # (The loop also clears at turn boundaries; this is per-tool-call
        # extra defensiveness.)
        from ..ui_tools import (
            get_ui_actions,
            reset_ui_actions,
        )

        before = list(get_ui_actions() or [])  # save what was already there
        try:
            # Validate args against the Vanna tool's Pydantic model.
            args_cls = self._tool.get_args_schema()
            try:
                validated = args_cls(**(args or {}))
            except Exception as exc:  # noqa: BLE001
                return AgentToolResult(
                    result_for_llm=(
                        f"ERROR: argument validation failed for {self.name!r}: "
                        f"{type(exc).__name__}: {exc}. "
                        f"Check the schema and retry with valid args."
                    ),
                    metadata={"validation_error": str(exc)},
                )

            # Build a minimal Vanna ToolContext. We pass through the
            # essentials; AgentMemory is replaced by a no-op stub so we
            # don't have to import + instantiate DemoAgentMemory just to
            # make a Pydantic field happy.
            vanna_ctx = _build_vanna_tool_context(ctx)

            try:
                result = await self._tool.execute(vanna_ctx, validated)
            except Exception as exc:  # noqa: BLE001
                return AgentToolResult(
                    result_for_llm=(
                        f"ERROR: tool {self.name!r} raised "
                        f"{type(exc).__name__}: {exc}."
                    ),
                    metadata={"exception": type(exc).__name__, "message": str(exc)},
                )

            # Drain side-channel ui_actions emitted during the call.
            after = get_ui_actions() or []
            new_actions = after[len(before):] if len(after) >= len(before) else list(after)

            # Vanna ToolResult → our ToolResult.
            result_for_llm = getattr(result, "result_for_llm", None) or ""
            metadata = dict(getattr(result, "metadata", None) or {})
            success = getattr(result, "success", True)
            if not success:
                metadata["vanna_success"] = False
                err = getattr(result, "error", None)
                if err:
                    metadata["vanna_error"] = str(err)

            return AgentToolResult(
                result_for_llm=str(result_for_llm),
                ui_actions=list(new_actions),
                metadata=metadata,
            )
        finally:
            # Don't reset_ui_actions here — the loop owns turn-level resets.
            pass


# ---------------------------------------------------------------------------
# Vanna ToolContext construction
# ---------------------------------------------------------------------------


def _build_vanna_tool_context(ctx: AgentToolContext) -> Any:
    """Build a minimal Vanna ToolContext from our ToolContext.

    Imports Vanna lazily so this module stays importable in environments
    where Vanna isn't installed (Phase 4+).
    """
    from .vanna_shim import User as VannaUser
    from .vanna_shim import ToolContext as VannaToolContext

    # Stub AgentMemory — Pydantic field requires the type, we don't use it.
    class _NoopMemory:
        def __getattr__(self, item: str) -> Any:
            return lambda *_a, **_kw: None

    vanna_user = VannaUser(
        id=f"{ctx.role}@homeos.local",
        group_memberships=[ctx.role],
    )
    # Stash the int user_id in metadata. The Vanna User.id field is typed
    # str ("admin@homeos.local") so we can't put the real int there
    # without breaking the Vanna contract — but ui_tools.UseSkillTool
    # reads metadata["user_id"] to populate SkillContext.user_id, which
    # is what every pending_actions / audit INSERT needs.
    return VannaToolContext.model_construct(
        user=vanna_user,
        conversation_id=ctx.conversation_id or "",
        request_id=ctx.request.request_id or uuid.uuid4().hex,
        agent_memory=_NoopMemory(),  # type: ignore[arg-type]
        metadata={
            "role":     ctx.role,
            "language": ctx.language,
            "user_id":  ctx.user_id,
        },
    )


# ---------------------------------------------------------------------------
# Bulk registration helper
# ---------------------------------------------------------------------------


def register_all_legacy_tools(registry: Any) -> int:
    """Register every Vanna tool from the legacy modules into a new-protocol
    ToolRegistry. Returns the count registered.

    Used by the Phase 1 wiring code to populate the new registry from
    the existing implementations with zero per-tool plumbing.
    """
    from ..ui_tools import (
        InstallConnectorTool,
        ListAppsTool,
        InvokeSkillTool,
        ListCalendarLayoutsTool,
        ListConnectorsTool,
        ShowCalendarTool,
        SkillViewTool,
        TriggerConnectorTool,
    )

    tools = [
        ShowCalendarTool(),
        ListCalendarLayoutsTool(),
        TriggerConnectorTool(),
        # Phase A1 (Hermes-style): ListSkillsTool removed. The in-prompt
        # {skill_index} block is the menu; list_skills was a searchable
        # duplicate that served as the model's #1 escape into exploration
        # mode. skill_view(name) reads a manifest, invoke_skill(name, args)
        # runs it — that's the whole skill surface.
        SkillViewTool(),
        InvokeSkillTool(),
        ListConnectorsTool(),
        InstallConnectorTool(),
        # search_documents migrated to backend/skills/search_documents/
        # — the skills registry loads it; do not double-register here.
        ListAppsTool(),
    ]
    count = 0
    for t in tools:
        try:
            registry.register(VannaToolAdapter(t))
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("could not adapt Vanna tool %r: %s", getattr(t, "name", "?"), exc)
    return count


__all__ = ["VannaToolAdapter", "register_all_legacy_tools"]
