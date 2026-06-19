"""Yorik Skills registry.

A skill is a discrete capability the LLM (or any caller) can dispatch
to by name. Each skill lives in backend/skills/<name>/ as:

  skill.md   ── YAML frontmatter (routing metadata) + markdown body
                (procedural instructions the LLM sees when invoking)
  skill.py   ── async def execute(ctx, **args) -> dict

Skills are loaded at FastAPI startup. The registry is the single source
of truth for "what can Yorik do?" — the chat agent's `use_skill` tool
dispatches via this registry, the Settings UI lists capabilities by
reading it, and HTTP endpoints become thin wrappers that delegate to
named skills (one capability → one implementation, never duplicated).

Cross-skill composition: every skill receives a SkillContext that
exposes `await ctx.call_skill(name, **args)`. So a `whatsapp_send_with_
photo` skill can transparently call `find_photo` then `whatsapp_send`,
and the agent's plan/execute loop stays clean.
"""

from .registry import (
    Registry,
    Skill,
    SkillContext,
    SkillError,
    load_all,
    get_registry,
)

__all__ = [
    "Registry",
    "Skill",
    "SkillContext",
    "SkillError",
    "load_all",
    "get_registry",
]
