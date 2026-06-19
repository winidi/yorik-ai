"""pick_compose_template — emit the template picker chat card.

Takes `candidates` (template_ids in best-first order), validates
against the template directory, dedupes, caps at 5, emits a
template_picker ui_action. Unknown ids are dropped silently with a
log entry so one hallucinated id doesn't break the whole picker.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("yorik.pick_compose_template")


async def execute(
    ctx,
    candidates: Optional[list[str]] = None,
) -> dict[str, Any]:
    from backend.compose import templates as _tpl_mod

    if not candidates or not isinstance(candidates, list):
        return {
            "ok": False,
            "_llm_hint": (
                "MISSING_CANDIDATES: pass `candidates` as a list of 1-5 "
                "template_ids in best-first order. Get the ids from a "
                "list_compose_templates call. Example: "
                "candidates=['generic-letter-en', 'generic-email-en', "
                "'invoice-en']."
            ),
        }

    # Load template index once, validate each candidate id, dedupe while
    # preserving best-first order. Unknown ids are dropped silently (with
    # a log entry) — we don't want to fail the whole picker just because
    # the LLM hallucinated one id out of three; the user still gets a
    # usable card with the surviving candidates.
    try:
        all_templates = _tpl_mod.load_all() or []
    except Exception as exc:  # noqa: BLE001
        log.warning("templates.load_all failed: %s", exc)
        all_templates = []

    by_id = {(t.get("id") or "").lower(): t for t in all_templates}

    seen: set[str] = set()
    rendered: list[dict[str, Any]] = []
    unknown: list[str] = []
    for raw in candidates:
        tid = str(raw or "").strip().lower()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        t = by_id.get(tid)
        if not t:
            unknown.append(str(raw))
            continue
        rendered.append({
            "id":          t.get("id") or "",
            "name":        t.get("name") or "",
            "description": t.get("description") or "",
            "kind":        t.get("kind") or "",
            "when_to_use": list(t.get("when_to_use") or []),
        })
        if len(rendered) >= 5:
            break  # picker UI caps at 5; refuse extras silently

    if unknown:
        log.info("pick_compose_template dropped unknown ids: %r", unknown)

    if not rendered:
        return {
            "ok": False,
            "unknown": unknown,
            "_llm_hint": (
                f"NO_VALID_CANDIDATES: all passed ids ({candidates!r}) "
                "are unknown. Call list_compose_templates first; only use "
                "ids that appear in its output."
            ),
        }

    from backend.ui_tools import _append
    _append({
        "type":      "template_picker",
        "templates": rendered,
    })

    rendered_ids = [r["id"] for r in rendered]
    unknown_note = f" (dropped unknown: {unknown!r})" if unknown else ""
    hint = (
        f"Template picker rendered with {len(rendered_ids)} candidate(s): "
        f"{rendered_ids}{unknown_note}. Reply ONE short sentence in the "
        "user's language asking them to pick (e.g. 'Welche Vorlage passt?'). "
        "Do NOT enumerate the templates in prose. Wait for a "
        "`[template_picked id=…]` follow-up message — that's when you "
        "proceed with compose_check_recipient / compose_draft using the "
        "chosen template_id."
    )
    return {
        "ok":        True,
        "rendered":  rendered_ids,
        "unknown":   unknown,
        "_llm_hint": hint,
    }
