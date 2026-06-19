"""SkillTool — wraps a Skill as a top-level OpenAI tool.

Each registered SkillTool becomes its own entry in the LLM's `tools`
array, with the skill's `inputs:` frontmatter compiled into the
`parameters` JSON Schema. The model then sees `add_calendar_event`,
`find_contact`, etc. directly — no `use_skill(name="X", args={...})`
indirection, no guessing args from a schema it never received.

Benefits over the use_skill+list_skills pattern:
  - Args are typed at the OpenAI API level. The model can't pass
    `color=...` to add_calendar_event because the schema doesn't
    include it; the API rejects unknown fields before they reach
    the skill code.
  - The skill's `description` + `when_to_use` from skill.md travels
    with the tool definition, replacing the need to keep
    skill-specific playbooks in the main system prompt.
  - Per-turn tool ACLs (admin-only mutations, viewer read-only,
    etc.) become a simple .schemas(names=allowed) filter on the
    registry.

For back-compat we keep UseSkillTool/ListSkillsTool registered too —
skills that haven't been promoted to top-level still work via that
path.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..agent.context import ToolContext
from ..agent.tools import ToolResult, schema_from_skill_inputs

from . import SkillContext, SkillError, get_registry
from .registry import Skill

log = logging.getLogger("yorik.skill_tool")

# Cap on how much of skill.md `when_to_use` we inline into the tool
# description. The Hermes-pattern target is ~2K chars per tool — that
# fits a real workflow playbook (POSTAL LETTER FLOW, calendar provider
# resolution, etc.) without ballooning the tools array. Originally 600
# which was too restrictive: load-bearing workflow rules couldn't fit,
# and we had to keep them duplicated in the main system prompt.
_WHEN_TO_USE_CAP = 4000


def _summarise_when_to_use(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= _WHEN_TO_USE_CAP:
        return text
    # Trim at a paragraph or line boundary if possible — never mid-word.
    head = text[:_WHEN_TO_USE_CAP]
    last_para = head.rfind("\n\n")
    if last_para > _WHEN_TO_USE_CAP // 2:
        return head[:last_para].rstrip() + " […]"
    last_line = head.rfind("\n")
    if last_line > _WHEN_TO_USE_CAP // 2:
        return head[:last_line].rstrip() + " […]"
    return head.rstrip() + "…"


class SkillTool:
    """In-tree Tool that exposes a single Skill as a top-level OpenAI tool.

    Implements the duck-typed Tool protocol from backend/agent/tools.py:
    `name`, `description`, `json_schema`, and async `execute(ctx, args)`.
    """

    def __init__(self, skill: Skill):
        self._skill = skill
        self._cached_schema: Optional[Dict[str, Any]] = None

    @property
    def name(self) -> str:
        return self._skill.name

    @property
    def description(self) -> str:
        parts: List[str] = []
        desc = (self._skill.description or "").strip()
        if desc:
            parts.append(desc)
        wtu = _summarise_when_to_use(self._skill.when_to_use)
        if wtu:
            parts.append("When to use:\n" + wtu)
        return "\n\n".join(parts) or self._skill.name

    @property
    def json_schema(self) -> Dict[str, Any]:
        if self._cached_schema is None:
            self._cached_schema = schema_from_skill_inputs(self._skill.inputs or {})
        return self._cached_schema

    async def execute(self, ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
        # The in-tree ToolContext exposes user_id as an int property
        # (agent/context.py line 75). Pass it through to SkillContext so
        # owner_user_id columns + pending_actions.user_id work correctly.
        reg = get_registry()
        skill_ctx = SkillContext(
            reg,
            role=getattr(ctx, "role", None) or "admin",
            user_id=getattr(ctx, "user_id", None),
            conversation_id=getattr(ctx, "conversation_id", None),
        )

        # Snapshot the side-channel ui_actions buffer before the call so
        # we can pack just THIS call's emissions into the ToolResult.
        # Without this, find_provider_nearby (and any skill that calls
        # ui_tools._append) silently loses its `pois_found` / `show_calendar`
        # / etc. card — the buffer holds them but the loop only forwards
        # what's on ToolResult.ui_actions. Same pattern vanna_adapter uses
        # for the legacy-tool path; SkillTool was missing it.
        from ..ui_tools import get_ui_actions
        before_actions = list(get_ui_actions() or [])

        try:
            result = await reg.invoke(self._skill.name, ctx=skill_ctx, **(args or {}))
        except SkillError as exc:
            return ToolResult(
                result_for_llm=f"Skill {self._skill.name!r} failed: {exc}",
                metadata={"skill_error": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("skill %s raised", self._skill.name)
            return ToolResult(
                result_for_llm=(
                    f"Skill {self._skill.name!r} raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                metadata={"exception": type(exc).__name__, "message": str(exc)},
            )

        # Drain ui_actions emitted DURING this skill call.
        after_actions = get_ui_actions() or []
        new_actions = (after_actions[len(before_actions):]
                       if len(after_actions) >= len(before_actions)
                       else list(after_actions))

        # When a skill returns `_llm_hint`, surface BOTH the hint (steering
        # rule) AND the structured data (truth). Earlier versions exposed
        # only the hint to prevent the LLM from echoing dict-text into the
        # user's reply — but that left briefing-style asks ("Überblick über
        # heute") with no data to summarise, so the LLM hallucinated. The
        # hint already tells the LLM how to phrase the reply; including the
        # raw data behind it lets it stay honest when it must enumerate.
        if isinstance(result, dict) and result.get("_llm_hint"):
            hint = str(result["_llm_hint"])
            data = {k: v for k, v in result.items() if k != "_llm_hint"}
            if data:
                preview = (
                    f"{hint}\n\n"
                    f"Source data (quote only what is here — do not invent rows):\n"
                    f"{_compact_json(data, max_chars=1500)}"
                )
            else:
                preview = hint
        elif isinstance(result, dict):
            preview = str(result)
            if len(preview) > 800:
                preview = preview[:800] + "…(truncated)"
        else:
            preview = str(result)
        return ToolResult(result_for_llm=preview, ui_actions=list(new_actions))


def _compact_json(data: Any, *, max_chars: int = 1500) -> str:
    """Render a skill's structured data for the LLM, truncated.

    Used by the `_llm_hint`-paired result path so the LLM has source data
    behind the steering rule. JSON keeps key/value structure visible
    (vs. str(dict) Python repr) which the model parses more reliably."""
    import json as _json
    try:
        s = _json.dumps(data, default=str, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        s = str(data)
    if len(s) > max_chars:
        s = s[: max_chars - 16] + "…(truncated)"
    return s


def register_skills_as_tools(registry: Any, skill_names: List[str]) -> int:
    """Register the named skills as top-level tools on `registry`.

    Skips skills that aren't loaded (logged warning, no raise) so a
    missing skill in the promotion list doesn't block startup. Returns
    the count actually registered.
    """
    skill_registry = get_registry()
    registered = 0
    for name in skill_names:
        skill = skill_registry.get(name)
        if skill is None:
            log.warning("skill_tool: skill %r not loaded, skipping top-level registration", name)
            continue
        try:
            registry.register(SkillTool(skill))
            registered += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("skill_tool: failed to register %r as top-level tool: %s", name, exc)
    log.info("skill_tool: registered %d/%d skills as top-level tools", registered, len(skill_names))
    return registered
