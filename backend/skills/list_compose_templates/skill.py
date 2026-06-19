"""list_compose_templates — compact one-line index of every Compose template.

Mirrors list_skills: returns id + kind + tags + description per template,
one line each, as a single text block. The LLM scans the list, picks 3
candidates by description, and calls pick_compose_template — or
view_compose_template(id=X) first when two siblings need disambiguation.

The full when_to_use / when_to_not_use bullets live in
view_compose_template; keeping them out of the list avoids the
truncation problem that hid most ids before (the wrapper cuts at 1.5k
chars, and 13 templates × full prose was ~11.6k chars).
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("yorik.list_compose_templates")


async def execute(ctx) -> dict[str, Any]:
    from backend.compose import templates as _tpl_mod
    try:
        all_templates = _tpl_mod.load_all() or []
    except Exception as exc:  # noqa: BLE001
        log.warning("templates.load_all failed: %s", exc)
        all_templates = []

    chosen = sorted(
        all_templates,
        key=lambda t: (
            (t.get("kind") or "").lower(),
            (t.get("name") or "").lower(),
        ),
    )

    lines: list[str] = []
    for t in chosen:
        tid = t.get("id") or ""
        kind = (t.get("kind") or "").lower()
        # Drop the kind tag if it just repeats `kind` ("letter", "email",
        # "invoice"); keep the rest in source order.
        tags = [str(x) for x in (t.get("tags") or []) if str(x).lower() != kind]
        paren_bits = [kind] + tags if kind else tags
        paren = ", ".join(paren_bits)
        desc = (t.get("description") or "").strip()
        lines.append(f"{tid}({paren}) — {desc}" if paren else f"{tid} — {desc}")

    body = "\n".join(lines) if lines else "(no templates installed)"
    hint = (
        f"{len(lines)} Compose templates available:\n"
        f"{body}\n\n"
        "Pick 1-3 ids whose description matches the user's intent best, then "
        "call pick_compose_template(candidates=[id1, id2, id3]) in best-first "
        "order. If two siblings look equally plausible, call "
        "view_compose_template(template_id=X) on each to read the "
        "when_to_use / when_to_not_use bullets before deciding. Use ONLY "
        "ids from the list above — do not invent ids. Do not call "
        "compose_draft directly; the user picks via the chat card."
    )

    return {"_llm_hint": hint}
