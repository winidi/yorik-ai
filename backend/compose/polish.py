"""Compose draft polishing — one LLM call to convert user freetalk into
a template-respecting body_text + optional Betreff.

Backs the "Yorik formuliert für mich" sparkle button on the
NeedsInputCard form. The user types whatever they want to say (often
keyword-shorthand: "schoenes wetter, viel sonne, wir kommen sonntag
zurueck"), the LLM expands it into a clean draft body that follows
the template's per-field `llm_hint` (no greeting, \\n\\n between
paragraphs, no AI filler, match user's wording).

Separate code path from compose_draft because:
  - Compose_draft is "save what I have"; polish is "rewrite what I
    started." Stuffing the LLM call into compose_draft would slow the
    common case (LLM already pre-filled body_text via the agent loop).
  - Polish is opt-in from the form, has its own UX (spinner, error
    message), and gets its own short single-shot LLM call with the
    template hint inlined — no agent loop overhead.

Reuses the same template's `ask_user_for_args[i].llm_hint` so the
rules don't drift between the agent-side pre-fill and the form-side
polish — single source of truth lives on the template.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

from . import templates as _tpl_mod

log = logging.getLogger("yorik.compose.polish")


# Roles we accept for polishing. Mirrors compose_check_template_args's
# _INTENT_ROLES — keeps the contract that "what counts as intent-derived"
# lives in one place across the codebase. from_intent=true is the
# explicit opt-in for templates that don't fit the role enum.
_INTENT_ROLES = {"body", "freeform_text"}


# Generic fallback rules when the template field has no llm_hint
# authored. Same shape the shipped generic-letter / generic-email
# templates carry, in case a custom template forgets to add it.
_FALLBACK_HINT = (
    "Write 1-3 short paragraphs of the requested content. "
    "NEVER include a greeting/Anrede or a closing — the template's "
    "anrede and gruss fields print those. NEVER include a signature "
    "or your name. Use \\n\\n between paragraphs — never one giant "
    "prose blob. Stay close to the user's wording and tone; only fix "
    "obvious typos and minor grammar. Do NOT pad with filler like "
    "'Hope you're doing well' or 'Let me know if you want details' — "
    "if the user wrote one sentence, you write one sentence."
)


class PolishError(ValueError):
    """User-recoverable problem (unknown template, non-intent field, …)."""


def _find_field(template: Dict[str, Any], field_key: str) -> Optional[Dict[str, Any]]:
    for entry in template.get("ask_user_for_args") or []:
        if isinstance(entry, dict) and entry.get("key") == field_key:
            return entry
    return None


def _betreff_field(template: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the subject/betreff field metadata if the template has one.
    Recognises both German `betreff` and English `subject` keys plus
    anything tagged role=subject so a template author can rename freely."""
    for entry in template.get("ask_user_for_args") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("role") == "subject":
            return entry
        if entry.get("key") in ("betreff", "subject"):
            return entry
    return None


def _build_prompt(
    *,
    intent: str,
    field_hint: str,
    template_name: str,
    recipient_name: Optional[str],
    suggest_betreff: bool,
    betreff_label: Optional[str],
    language: str,
) -> str:
    """Compose the single-shot prompt. Inlines:
      - the user's intent verbatim (the thing to polish)
      - the template's field-level llm_hint (the rules to follow)
      - recipient name when known (so the LLM doesn't invent a name)
      - whether to also propose a Betreff
    No agent loop, no tool catalog, no skill index — just one call.
    """
    recipient_line = (
        f"Recipient: {recipient_name} (use their name only if the rules say to, "
        "do NOT inject a greeting)."
        if recipient_name else
        "Recipient name unknown — write neutrally; do NOT invent one."
    )
    betreff_block = (
        f"\n  - {betreff_label or 'betreff'}: a short subject line capturing "
        "the topic — match the body's register, max 6 words, no trailing "
        "punctuation. Empty string \"\" if the body has no clear topic."
        if suggest_betreff else ""
    )
    return (
        "You are polishing one field of a compose draft. Strict JSON output, "
        "no prose before or after, no markdown fences.\n\n"
        f"Template: {template_name}\n"
        f"{recipient_line}\n"
        f"User wants to write (verbatim, may be terse / keyword-shorthand / "
        f"typo'd): \"{intent.strip()}\"\n\n"
        "FIELD RULES (template-author-supplied — follow exactly):\n"
        f"{field_hint}\n\n"
        f"Reply in {language}.\n\n"
        "OUTPUT FORMAT (strict JSON):\n"
        "{\n"
        '  "body_text": "the polished body, paragraphs separated by \\n\\n"'
        f"{betreff_block}\n"
        "}\n"
    )


def _parse_response(content: str, suggest_betreff: bool) -> Dict[str, Any]:
    """Tolerant JSON parse — strip fences, find first {…} block. Returns
    {body_text, betreff?} with empty defaults so caller can always read
    body_text without KeyError chasing."""
    s = (content or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    parsed: Dict[str, Any]
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return {"body_text": "", "betreff": None}
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {"body_text": "", "betreff": None}
    if not isinstance(parsed, dict):
        return {"body_text": "", "betreff": None}
    body = (parsed.get("body_text") or "").strip()
    betreff = None
    if suggest_betreff:
        b = (parsed.get("betreff") or parsed.get("subject") or "").strip()
        if b:
            betreff = b
    return {"body_text": body, "betreff": betreff}


def polish(
    *,
    intent: str,
    template_id: str,
    field_key: str = "body_text",
    contact_id: Optional[int] = None,
    suggest_betreff: bool = True,
    language: str = "German",
) -> Dict[str, Any]:
    """Convert `intent` into a polished `body_text` (and optionally a
    suggested betreff) following the template's per-field llm_hint.

    Raises PolishError for user-recoverable problems (unknown template,
    field not intent-derived) so the route handler can translate to a
    400 with a readable message.
    """
    intent = (intent or "").strip()
    if not intent:
        raise PolishError("intent is empty — nothing to polish")

    # Resolve template + field. Both lookups raise PolishError on miss
    # so the caller gets a clean 400 instead of a 500.
    try:
        template = _tpl_mod.get(template_id)
    except _tpl_mod.TemplateError as exc:
        raise PolishError(str(exc)) from exc
    field = _find_field(template, field_key)
    if not field:
        raise PolishError(
            f"template {template_id!r} has no ask_user_for_args entry "
            f"for key={field_key!r}"
        )
    role = (field.get("role") or "").lower()
    if role not in _INTENT_ROLES and not field.get("from_intent"):
        raise PolishError(
            f"field {field_key!r} on template {template_id!r} is not "
            f"intent-derived (role={role!r}, from_intent={field.get('from_intent')!r}); "
            "polish only applies to body / freeform_text fields"
        )

    # Hint resolution: per-field llm_hint > fallback. Both authored
    # and fallback paths use the same shape so the LLM prompt stays
    # uniform.
    field_hint = (field.get("llm_hint") or "").strip() or _FALLBACK_HINT

    # Recipient name (if available) — purely for register-awareness;
    # the prompt rules explicitly forbid inserting it as a greeting.
    recipient_name: Optional[str] = None
    if contact_id:
        try:
            from backend import contacts as C
            c = C.get(int(contact_id))
            if c:
                recipient_name = (
                    c.get("first_name") or c.get("display_name") or None
                )
        except Exception as exc:  # noqa: BLE001 — lookup is best-effort
            log.debug("polish: contact lookup failed: %s", exc)

    # Betreff field exists on the template?
    betreff_field = _betreff_field(template)
    will_suggest = bool(suggest_betreff and betreff_field is not None)
    betreff_label = (betreff_field.get("label") if betreff_field else None) or "betreff"

    prompt = _build_prompt(
        intent=intent,
        field_hint=field_hint,
        template_name=template.get("name") or template_id,
        recipient_name=recipient_name,
        suggest_betreff=will_suggest,
        betreff_label=betreff_label,
        language=language or "German",
    )

    # Single-shot call. Same client the contact_extractor uses.
    from backend.agent.llm import LlmClient
    client = LlmClient(
        model=os.getenv("HOMEOS_MODEL", "qwen3.5-9b"),
        base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
    )
    try:
        resp = client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.3,
        )
    except Exception as exc:  # noqa: BLE001 — converted to 500 by caller
        log.warning("polish LLM call failed: %s", exc)
        raise

    content = (resp.get("content") or "").strip()
    out = _parse_response(content, will_suggest)
    # Empty body usually means malformed JSON or refusal — surface that
    # so the UI can prompt the user to rephrase rather than silently
    # clobbering their typed text with "".
    if not out.get("body_text"):
        raise PolishError(
            "LLM returned no usable body_text — try rephrasing your intent"
        )
    return out
