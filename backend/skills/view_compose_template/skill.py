"""view_compose_template — read full prose for ONE Compose template.

Mirrors skill_view: takes a single template_id, returns the full
description + when_to_use + when_to_not_use bullets + metadata so the
LLM can disambiguate between sibling candidates before calling
pick_compose_template.

list_compose_templates returns only the compact one-liner per template;
when two ids look equally plausible (e.g. kuendigung-mietvertrag-de vs
kuendigung-vertrag-allgemein-de), call this skill on each before deciding.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("yorik.view_compose_template")


async def execute(ctx, *, template_id: Optional[str] = None) -> dict[str, Any]:
    tid = (template_id or "").strip()
    if not tid:
        return {
            "_llm_hint": (
                "MISSING_TEMPLATE_ID: view_compose_template requires "
                "`template_id`. Call list_compose_templates first; pass one "
                "of the ids from its output."
            ),
        }

    from backend.compose import templates as _tpl_mod
    try:
        t = _tpl_mod.get(tid)
    except _tpl_mod.TemplateError:
        return {
            "_llm_hint": (
                f"UNKNOWN_TEMPLATE_ID: no template named {tid!r}. Call "
                "list_compose_templates to see the real ids — only use ones "
                "from its output."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("templates.get(%r) failed: %s", tid, exc)
        return {
            "_llm_hint": (
                f"ERROR reading template {tid!r}: {type(exc).__name__}. "
                "Try a different id from list_compose_templates."
            ),
        }

    view = {
        "id":              t.get("id") or "",
        "name":            t.get("name") or "",
        "kind":            t.get("kind") or "",
        "description":     t.get("description") or "",
        "tags":            list(t.get("tags") or []),
        "vertical":        t.get("vertical") or "",
        "when_to_use":     list(t.get("when_to_use") or []),
        "when_to_not_use": list(t.get("when_to_not_use") or []),
    }

    rendered = json.dumps(view, ensure_ascii=False, indent=2)
    hint = (
        f"Full prose for template {tid!r}:\n{rendered}\n\n"
        "Read when_to_use vs when_to_not_use to confirm fit. If this is the "
        "right template, call pick_compose_template(candidates=[this_id, "
        "...]) — first element is the top pick. If not, view a sibling or "
        "go back to list_compose_templates."
    )
    return {"_llm_hint": hint}
