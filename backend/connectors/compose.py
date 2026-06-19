"""Compose connector — LLM-callable document drafting + revision.

Two operations the LLM uses for voice-driven document creation:

  draft       — pick a template + collect arguments + render the body. Emits
                an `open_compose_draft` UI action so the frontend switches to
                the Compose app and loads the draft into the TipTap editor.
  revise      — rewrite a selected passage per the user's instruction.
                Returns up to 3 candidate replacements with rationale, for
                the highlight-and-ask diff-accept UI.

Both ops talk to the same local LLM (Qwen3 via the in-tree agent loop
in `backend.ask` / `backend.agent`) so revisions stay on-device.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from . import ConnectorSpec, register
from ..compose import templates as tpl
from ..compose import render as rdr

log = logging.getLogger("homeos.connectors.compose")


def emit_ui_action(action):
    """Defer the ui_tools import to call time to break the circular
    import chain (connectors → ui_tools → ask → connectors)."""
    from ..ui_tools import _append
    _append(action)


# ─── draft ────────────────────────────────────────────────────────────────

async def _do_draft(template_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        template = tpl.get(template_id)
    except tpl.TemplateError as exc:
        return {"ok": False, "error": str(exc)}
    rendered = await rdr.render_template(template, args or {})
    # Emit a UI action telling the frontend to switch to Compose and load
    # this draft into the editor. The action carries the rendered html +
    # source data so the AI panel can populate immediately.
    emit_ui_action({
        "type": "open_compose_draft",
        "template_id": template_id,
        "template_name": template.get("name"),
        "html": rendered["html"],
        "data": _slim_data(rendered["data"]),
        "args": args or {},
    })
    return {
        "ok": True,
        "template_id": template_id,
        "template_name": template.get("name"),
        "preview_chars": len(rendered["html"]),
    }


def _slim_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Trim huge per-op responses down to a JSON-friendly preview so the
    UI action payload doesn't bloat. We keep the structure, just shorten."""
    def shrink(v: Any, depth: int = 0) -> Any:
        if depth > 3:
            return "…"
        if isinstance(v, dict):
            return {k: shrink(x, depth + 1) for k, x in list(v.items())[:30]}
        if isinstance(v, list):
            return [shrink(x, depth + 1) for x in v[:10]] + (["…+more"] if len(v) > 10 else [])
        if isinstance(v, str) and len(v) > 240:
            return v[:240] + "…"
        return v
    return {k: shrink(v) for k, v in (data or {}).items()}


# ─── revise ────────────────────────────────────────────────────────────────

# Length-aware prompt + token budget. The "short" defaults match the old
# behaviour — 1-3 terse alternatives, ~220 tokens. "medium" and "long"
# unlock the LLM to actually write paragraph-length replacements; without
# these the user got cropped 3-sentence "candidate replacements" even
# when they asked for a fully-written letter body.
_LENGTHS: Dict[str, Dict[str, Any]] = {
    "short": {
        "max_tokens": 220,
        "guidance": (
            "Reply with 1-3 short alternative phrasings, one per line, no numbering, "
            "no quotes, no commentary. Each replacement should be roughly the same "
            "length as the selected passage."
        ),
    },
    "medium": {
        "max_tokens": 500,
        "guidance": (
            "Reply with 1-3 alternative versions, one per line, no numbering, no quotes, "
            "no commentary. Each should be 2-4 sentences — fleshed out beyond a one-liner "
            "but still concise."
        ),
    },
    "long": {
        "max_tokens": 1500,
        "guidance": (
            "Reply with ONE fully expanded version. Aim for 1-3 paragraphs of substantive "
            "prose. Do NOT include alternative versions, numbering, quotes, or commentary "
            "— the user wants a single ready-to-use replacement they can paste in."
        ),
    },
}


_REVISE_SYSTEM_BASE = """\
You revise passages of business documents per a user instruction.
Keep the same language as the input. Preserve factual numbers, names,
dates, and amounts unless the instruction says otherwise. When you are
given recipient / sender facts in the CONTEXT FACTS block, USE THE
ACTUAL NAMES instead of writing placeholders like [Name], [Dein Name],
[Recipient], or [Empfänger] — those will be reported as bugs.
"""

# When the frontend detects that the user selected a template placeholder
# (parens-wrapped stub, "Hier kommt …" prose, etc.) it sends mode="write".
# The SELECTED PASSAGE is then meaningless context — using it as input
# produced garbled output because the LLM tried to "preserve" the stub's
# meaning. This override tells the LLM to ignore the selection and
# generate fresh content matching the instruction.
_WRITE_SYSTEM_OVERRIDE = """\
You are writing fresh content for a business document. The SELECTED
PASSAGE is a TEMPLATE PLACEHOLDER (e.g. "(Hier kommt dein Brieftext
rein …)") — IGNORE IT ENTIRELY. Do NOT try to preserve its meaning,
structure, or any words from it. Generate new content matching the
INSTRUCTION and CONTEXT FACTS. Match the language of the surrounding
document or the instruction itself. Use real names from CONTEXT FACTS
— never write [Name] / [Dein Name] / [Recipient] placeholders.
"""


# Greetings / closings the LLM should NOT re-add when the surrounding
# document already has them. Without this guard the LLM adds "Hallo X,"
# at the start of the revised passage even when the line right above
# the selection is already "Hallo X," — producing stacked greetings.
# Same shape for closings ("Viele Grüße" + "Mit freundlichen Grüßen"
# stacked). DE + EN patterns; word-boundary anchored so "Cheers!" or
# "best regards" inside body prose don't false-positive on every
# document containing those words mid-sentence.
import re as _re
_GREETING_PATTERNS = [
    _re.compile(p, _re.IGNORECASE) for p in (
        r"\bsehr\s+geehrte[rsn]?\b",
        r"\bguten\s+(tag|morgen|abend)\b",
        r"\bliebe[rs]?\s+[A-ZÄÖÜ]",       # "Liebe Anna" — must be followed by a name
        r"^\s*hallo\b",                    # only at line start to avoid mid-prose
        r"\n\s*hallo\b",
        r"^\s*hi\s+[A-ZÄÖÜ]",
        r"\n\s*hi\s+[A-ZÄÖÜ]",
        r"^\s*dear\s+[A-ZÄÖÜ]",
        r"\n\s*dear\s+[A-ZÄÖÜ]",
        r"^\s*hello\s+[A-ZÄÖÜ]",
        r"\n\s*hello\s+[A-ZÄÖÜ]",
    )
]
_CLOSING_PATTERNS = [
    _re.compile(p, _re.IGNORECASE) for p in (
        r"\b(mit\s+)?freundlichen\s+grüßen\b",
        r"\b(viele|beste|herzliche|liebe)\s+grüße\b",
        r"\bbest\s+regards\b",
        r"\bkind\s+regards\b",
        r"\bsincerely(\s+yours)?\b",
        r"\bregards,\b",
    )
]


def _detect_chrome(before: str, after: str) -> Dict[str, bool]:
    """Look for existing greetings/closings in the surrounding context
    so the prompt can tell the LLM not to add another one. Both regions
    checked because a greeting could be in `before` (most common — the
    user selected a middle paragraph) or, less often, in `after` (the
    user selected the very first paragraph but a duplicate greeting
    landed there). Same for closings."""
    return {
        "greeting_before": any(p.search(before or "") for p in _GREETING_PATTERNS),
        "greeting_after":  any(p.search(after or "")  for p in _GREETING_PATTERNS),
        "closing_before":  any(p.search(before or "") for p in _CLOSING_PATTERNS),
        "closing_after":   any(p.search(after or "")  for p in _CLOSING_PATTERNS),
    }


def _format_context_facts(facts: Optional[Dict[str, Any]]) -> str:
    """Render the frontend-supplied facts dict as a tight prompt block.
    Empty / None values are skipped so the prompt doesn't say things
    like `recipient_name: None`."""
    if not facts:
        return ""
    lines = []
    label_map = {
        "kind":              "Document kind",
        "recipient_name":    "Recipient name",
        "recipient_address": "Recipient address",
        "sender_name":       "Sender name",
        "sender_business":   "Sender business",
        "subject":           "Subject",
        "language":          "Language",
    }
    for k, label in label_map.items():
        v = facts.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        lines.append(f"  {label}: {s}")
    if not lines:
        return ""
    return "CONTEXT FACTS:\n" + "\n".join(lines) + "\n\n"


def _call_llm_for_revise(selected: str, instruction: str,
                          before: str = "", after: str = "",
                          length: str = "short",
                          context_facts: Optional[Dict[str, Any]] = None,
                          mode: str = "revise") -> Dict[str, Any]:
    """Synchronous LLM call for revisions. Reuses the same Qwen3 endpoint
    the agent uses; latency scales with `length` (~500ms short, ~3-5s long).

    `mode="write"` swaps the system base prompt to a write-from-scratch
    variant — used when the frontend detected the selection is a
    template placeholder and the LLM should ignore it.

    Returns `{suggestions, llm_offline, error}` so the dispatch can pass
    the offline reason through to the UI — previously this silently
    returned `[]` and the user saw a generic "no suggestions came back"
    when the real cause was the LLM being down.
    """
    from .. import ask as vanna_agent  # local: avoid import cycle at registration
    base_url = getattr(vanna_agent, "LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    model = getattr(vanna_agent, "LLM_MODEL", "qwen3.6-27b-mtp")
    import requests
    length = (length or "short").lower().strip()
    if length not in _LENGTHS:
        length = "short"
    cfg = _LENGTHS[length]
    mode = (mode or "revise").lower().strip()
    base_prompt = _WRITE_SYSTEM_OVERRIDE if mode == "write" else _REVISE_SYSTEM_BASE
    # Chrome-detection guard: if the surrounding context already has a
    # greeting/closing, tell the LLM explicitly not to duplicate it.
    # Without this, Qwen3 stacks "Hallo Max," on top of an existing
    # "Hallo Max," because the LLM defaults to "be polite, add a
    # greeting" without checking what's already there.
    chrome = _detect_chrome(before, after)
    chrome_rules: list[str] = []
    if chrome["greeting_before"] or chrome["greeting_after"]:
        chrome_rules.append(
            "GREETING ALREADY PRESENT in the surrounding text — do NOT add "
            "another greeting (Hallo, Sehr geehrte, Liebe, Dear, Hi, …) to "
            "your output. Start mid-sentence / mid-paragraph."
        )
    if chrome["closing_before"] or chrome["closing_after"]:
        chrome_rules.append(
            "CLOSING ALREADY PRESENT in the surrounding text — do NOT add "
            "another closing (Viele Grüße, Mit freundlichen Grüßen, Best "
            "regards, …) to your output. End on the last content sentence."
        )
    chrome_block = ("\n" + "\n".join(chrome_rules)) if chrome_rules else ""
    system_msg = base_prompt + "\n" + cfg["guidance"] + chrome_block
    prompt_ctx = _format_context_facts(context_facts)
    if before: prompt_ctx += f"Context BEFORE: {before[-200:]}\n"
    if after:  prompt_ctx += f"Context AFTER:  {after[:200]}\n"
    user_msg = f"{prompt_ctx}\nSELECTED PASSAGE:\n{selected}\n\nINSTRUCTION:\n{instruction}"
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.4,
                "max_tokens": int(cfg["max_tokens"]),
                # llama.cpp expects this as a TOP-LEVEL field, not nested
                # under extra_body (that's an OpenAI-SDK convenience). Without
                # this Qwen3 burns the whole budget on reasoning tokens and
                # returns an empty visible string. Same fix as QwenLlmService.
                "chat_template_kwargs": {"enable_thinking": False},
                # Ollama's compat endpoint ignores chat_template_kwargs;
                # reasoning_effort: "none" is the field it honors. See
                # agent/llm.py for the full rationale.
                "reasoning_effort": "none",
            },
            # Short timeouts: connect should be near-instant on localhost,
            # read needs to cover model warmup + the longer "long" budget.
            timeout=(2, 60 if length == "long" else 15),
        )
        r.raise_for_status()
        text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout) as exc:
        log.warning("revise LLM call failed (offline?): %s", exc)
        return {"suggestions": [], "llm_offline": True,
                "error": f"LLM endpoint at {base_url} is unreachable. Start your local LLM and try again."}
    except Exception as exc:  # noqa: BLE001
        log.exception("revise LLM call failed: %s", exc)
        return {"suggestions": [], "error": f"{type(exc).__name__}: {exc}"}
    # For "long" we asked for ONE coherent paragraph block — treat the
    # whole response as a single suggestion (newlines are paragraph breaks,
    # not separators). For short/medium, newline-separated alternatives.
    if length == "long":
        stripped = text.strip()
        out = [{"text": stripped, "rationale": ""}] if stripped else []
    else:
        suggestions = [line.strip() for line in text.splitlines() if line.strip()]
        out = [{"text": s, "rationale": ""} for s in suggestions[:3]
               if len(s) >= max(3, len(selected) // 8)]
    return {"suggestions": out}


_WRITE_ARG_SYSTEM = """\
You are writing fresh content for ONE specific field of a business
document. The user told you which field (FIELD), gave you facts
about the document (CONTEXT FACTS), and what they want written
(INSTRUCTION). Reply with ONLY the field's content — no field name,
no commentary, no markdown fences, no greeting/closing chrome unless
the field IS a greeting/closing. Use the actual names from CONTEXT
FACTS — never write [Name], [Dein Name], [Recipient], etc. Match
the language of the instruction (or the recipient's language if the
instruction is ambiguous).
"""


def _call_llm_for_write_arg(*, target_arg_key: str, arg_label: str,
                             current_value: str, instruction: str,
                             length: str = "medium",
                             context_facts: Optional[Dict[str, Any]] = None,
                             document_body: str = "",
                             target_arg_role: str = "",
                             ) -> Dict[str, Any]:
    """LLM call for the structural "write this specific template arg"
    flow. No selection, no context_before/after — the LLM is told
    exactly which field it is writing and what facts to use. Returns
    {suggestions: [{text}], llm_offline?, error?} for parity with
    _call_llm_for_revise. Always one suggestion (the full written
    content), regardless of length preset.
    """
    from .. import ask as vanna_agent
    base_url = getattr(vanna_agent, "LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    model = getattr(vanna_agent, "LLM_MODEL", "qwen3.6-27b-mtp")
    import requests
    length = (length or "medium").lower().strip()
    if length not in _LENGTHS:
        length = "medium"
    cfg = _LENGTHS[length]
    # For write_arg, even the "short" preset wants ONE coherent block
    # (the field's value), not 3 alternative phrasings — override the
    # guidance accordingly. Token budget still scales with the preset.
    if length == "short":
        length_guidance = "Keep it concise — 1-2 sentences is usually enough for a short field."
    elif length == "medium":
        length_guidance = "Aim for 2-4 sentences."
    else:
        length_guidance = "Aim for 1-3 substantive paragraphs."
    system_msg = _WRITE_ARG_SYSTEM + "\n" + length_guidance

    # Field-shape rules: by far the most common bug is "user asked to
    # write body_text, LLM added 'Sehr geehrte Damen und Herren,' at
    # the top — the template ALSO renders args.anrede separately so
    # the document ends up with two greetings stacked." Hard-coding
    # this per field-shape is more reliable than expecting the LLM
    # to infer it from the field name alone.
    #
    # `target_arg_role` is the canonical signal (set by the template
    # via ask_user_for_args.role — see templates/SCHEMA.md). When the
    # template hasn't migrated to declarative roles, fall back to a
    # key-name regex/closed-set check. Either path classifies the
    # field into exactly one of: body / greeting / closing / subject.
    role = (target_arg_role or "").lower().strip()
    key_lower = (target_arg_key or "").lower()
    if role:
        is_body_field    = role == "body"
        is_greeting_only = role == "greeting"
        is_closing_only  = role == "closing"
        is_subject_only  = role == "subject"
    else:
        is_body_field    = bool(_re.search(r"(^|_)(body|text|content|message|notes|brief)(_|$)", key_lower))
        is_greeting_only = key_lower in ("anrede", "greeting", "salutation")
        is_closing_only  = key_lower in ("gruss", "closing", "schlussformel")
        is_subject_only  = key_lower in ("betreff", "subject", "title")
    if is_body_field:
        system_msg += (
            "\n\nCRITICAL: This field is the BODY of the document. DO NOT "
            "include a greeting (Sehr geehrte Damen und Herren, Hallo, "
            "Liebe, Dear, Hi, ...), a closing (Mit freundlichen Grüßen, "
            "Viele Grüße, Best regards, Sincerely, ...), or a signature / "
            "sender name in your output. Those live in SEPARATE template "
            "fields (anrede, gruss, absender_name) and writing them here "
            "creates DUPLICATES the user will report as a bug. Start the "
            "first paragraph DIRECTLY with the substance of the message."
        )
    elif is_greeting_only:
        system_msg += (
            "\n\nThis field is ONLY the greeting line. Output ONE line like "
            "'Sehr geehrte Frau Müller,' / 'Hallo Max,' / 'Dear Anna,'. "
            "No body, no signature."
        )
    elif is_closing_only:
        system_msg += (
            "\n\nThis field is ONLY the closing line. Output ONE short "
            "phrase like 'Viele Grüße' / 'Mit freundlichen Grüßen' / "
            "'Best regards'. No body, no name."
        )
    elif is_subject_only:
        system_msg += (
            "\n\nThis field is ONLY the subject. Output ONE short line — "
            "no greeting, no body, no closing, no commentary."
        )

    ctx = _format_context_facts(context_facts)
    cur_block = ""
    if current_value and current_value.strip():
        cur_block = f"CURRENT VALUE (replace this entirely):\n{current_value.strip()[:1000]}\n\n"
    # Document body — needed for auto-generated subjects (the LLM has
    # to know what the letter is about to write a meaningful subject).
    # Capped so a long body doesn't blow the prompt budget.
    body_block = ""
    if document_body and document_body.strip():
        body_block = f"DOCUMENT BODY:\n{document_body.strip()[:3000]}\n\n"
    # If no explicit instruction was given (one-click auto-gen path),
    # provide a sensible default per field shape so the LLM has
    # something to anchor on.
    effective_instruction = instruction.strip() if instruction else ""
    if not effective_instruction:
        if is_subject_only:
            effective_instruction = (
                "Write a concise, descriptive subject line for the document body "
                "above. Match the document's language."
            )
        elif is_greeting_only:
            effective_instruction = (
                "Write a suitable greeting for the recipient named in CONTEXT FACTS."
            )
        elif is_closing_only:
            effective_instruction = (
                "Write a suitable closing phrase appropriate to the document's tone."
            )
        else:
            effective_instruction = (
                "Write content appropriate for this field based on the CONTEXT FACTS "
                "and DOCUMENT BODY."
            )
    user_msg = (
        f"FIELD: {arg_label} (template arg key: {target_arg_key})\n\n"
        f"{ctx}"
        f"{body_block}"
        f"{cur_block}"
        f"INSTRUCTION:\n{effective_instruction}"
    )
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": user_msg},
                ],
                "temperature": 0.4,
                "max_tokens": int(cfg["max_tokens"]),
                "chat_template_kwargs": {"enable_thinking": False},
                "reasoning_effort": "none",
            },
            timeout=(2, 60 if length == "long" else 15),
        )
        r.raise_for_status()
        text = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout) as exc:
        log.warning("write_arg LLM call failed (offline?): %s", exc)
        return {"suggestions": [], "llm_offline": True,
                "error": f"LLM endpoint at {base_url} is unreachable. Start your local LLM and try again."}
    except Exception as exc:  # noqa: BLE001
        log.exception("write_arg LLM call failed: %s", exc)
        return {"suggestions": [], "error": f"{type(exc).__name__}: {exc}"}
    stripped = text.strip()
    # Strip stray markdown fences the LLM sometimes adds despite the prompt.
    # `_re` is the module-level alias imported at the top of this file —
    # do NOT re-import locally here, that creates a function-scope shadow
    # which makes earlier `_re.search` references in this same function
    # raise UnboundLocalError.
    if stripped.startswith("```"):
        stripped = _re.sub(r"^```[a-z]*\n?", "", stripped, flags=_re.IGNORECASE)
        stripped = _re.sub(r"\n?```\s*$", "", stripped)
    out = [{"text": stripped, "rationale": ""}] if stripped else []
    return {"suggestions": out}


# ─── dispatch ─────────────────────────────────────────────────────────────

def compose(op: str,
            template_id: str = "",
            args: Optional[Dict[str, Any]] = None,
            selected_text: str = "",
            instruction: str = "",
            context_before: str = "",
            context_after: str = "",
            length: str = "short",
            context_facts: Optional[Dict[str, Any]] = None,
            mode: str = "revise",
            target_arg_key: str = "",
            arg_label: str = "",
            current_value: str = "",
            document_body: str = "",
            target_arg_role: str = "",
            **_kw) -> Dict[str, Any]:
    op = (op or "").lower().strip()

    if op == "list_templates":
        return {"templates": [tpl.public_dict(t) for t in tpl.load_all()]}

    if op == "draft":
        if not template_id:
            return {"ok": False, "error": "draft requires 'template_id' — call op=list_templates first to see options"}
        # Run the async pipeline. asyncio.run is fine here because the connector
        # dispatch wraps sync invokes in a thread, so there's no running loop.
        try:
            return asyncio.run(_do_draft(template_id, args or {}))
        except RuntimeError:
            # Already inside a loop — schedule as task and wait
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(_do_draft(template_id, args or {}))

    if op == "revise":
        if not selected_text or not instruction:
            return {"ok": False, "error": "revise requires 'selected_text' and 'instruction'"}
        log.info("compose revise: length=%s mode=%s sel_len=%d ctx_facts_keys=%s",
                 length, mode, len(selected_text or ""),
                 list((context_facts or {}).keys()))
        out = _call_llm_for_revise(
            selected_text, instruction, context_before, context_after,
            length=length, context_facts=context_facts, mode=mode,
        )
        # If the helper signalled an outage, pass that through with ok=False
        # so the UI can show a real reason instead of "no suggestions".
        if out.get("llm_offline"):
            return {"ok": False, "suggestions": [], "llm_offline": True,
                    "error": out.get("error", "LLM offline")}
        return {"ok": True, "suggestions": out.get("suggestions") or []}

    if op == "write_arg":
        # Structural alternative to selection-based revise: the frontend
        # knows the user wants to write/replace a specific template arg
        # (e.g. body_text), so there's no selected_text to disambiguate.
        # Eliminates the entire class of "did the LLM treat the stub as
        # context?" bugs because the LLM is told exactly which field it
        # is writing — no selection, no context_before/after.
        # `instruction` is optional — the helper supplies a per-field-
        # shape default for one-click auto-generate paths (e.g. clicking
        # "AI" on the subject field auto-writes a subject without making
        # the user type anything).
        if not target_arg_key:
            return {"ok": False, "error": "write_arg requires 'target_arg_key'"}
        log.info("compose write_arg: target=%s len=%s ctx_facts_keys=%s",
                 target_arg_key, length, list((context_facts or {}).keys()))
        out = _call_llm_for_write_arg(
            target_arg_key=target_arg_key,
            arg_label=arg_label or target_arg_key,
            current_value=current_value,
            instruction=instruction,
            length=length,
            context_facts=context_facts,
            document_body=document_body,
            target_arg_role=target_arg_role,
        )
        if out.get("llm_offline"):
            return {"ok": False, "suggestions": [], "llm_offline": True,
                    "error": out.get("error", "LLM offline")}
        return {"ok": True, "suggestions": out.get("suggestions") or []}

    return {"ok": False, "error": f"unknown op '{op}'. Use list_templates / draft / revise / write_arg."}


register(ConnectorSpec(
    name="compose",
    description=(
        "Draft a business document (invoice, quote, letter, consultation note) from a "
        "template, or revise a highlighted passage. PREFER op='draft' when the user "
        "says 'create an invoice for X', 'write a quote', 'draft a letter to Y about Z' — "
        "this opens the Compose app with a pre-filled draft. Use op='list_templates' "
        "first if you're unsure which template to pick. Use op='revise' for "
        "passage-level rewrites with explicit selection text + instruction."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["list_templates", "draft", "revise"]},
            "template_id": {"type": "string", "description": "Template to use, e.g. 'praxis-rechnung'"},
            "args": {"type": "object", "description": "Template arguments (e.g. {patient_id: 2, rechnungsnummer: '2026-185'})"},
            "selected_text": {"type": "string", "description": "Passage to revise (for op=revise)"},
            "instruction": {"type": "string", "description": "How to revise (for op=revise) — e.g. 'make this more formal', 'shorter', 'translate to English'"},
            "context_before": {"type": "string", "description": "Optional ~200 chars before the selection"},
            "context_after": {"type": "string", "description": "Optional ~200 chars after the selection"},
        },
        "required": ["op"],
    },
    invoke=compose,
    requires_auth=False,
    backend="builtin",
    version="1.0",
    tags=["compose", "documents", "ai", "drafting"],
))
