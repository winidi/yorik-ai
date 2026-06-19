"""compose_extract_args — LLM-driven field extraction from a text blob.

Powers the per-slot paste textarea + upload-document affordance in the
Compose args panel. The user dumps text (a pasted letter header, a
website snippet, OCR'd PDF content), the skill returns only the args
it can confidently map to the template's declared slots.

Multi-recipient templates pass `contact_group` so extracting for the
Vermieter slot doesn't leak into the Verwalter slot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, List, Optional

log = logging.getLogger("yorik.compose_extract_args")

# Trim the user's text before sending to the LLM — anything past this
# is almost certainly noise from a bulk paste. 12k chars ≈ 3 pages of
# dense text, plenty for any reasonable contact / address extraction.
_MAX_TEXT_CHARS = 12_000


def _scoped_arg_specs(
    template: dict[str, Any],
    contact_group: Optional[str],
) -> List[dict[str, Any]]:
    """Return the ask_user_for_args entries this call should extract for.

    When contact_group is set: only args declaring that group.
    When unset: all args from ask_user_for_args."""
    spec = template.get("ask_user_for_args") or []
    if not isinstance(spec, list):
        return []
    if contact_group is None:
        return [s for s in spec if isinstance(s, dict)]
    cg = contact_group.strip().lower()
    out = []
    for s in spec:
        if not isinstance(s, dict):
            continue
        if (s.get("contact_group") or "").strip().lower() == cg:
            out.append(s)
    return out


def _role_meaning(role: str) -> str:
    """Plain-English meaning of a role enum value. Surfaces to the LLM so
    similar-spelled keys (mieter_name vs vermieter_name) don't get mixed
    up. Without this, Qwen3 picked the closest-spelled key regardless of
    which side of the letter it actually represented."""
    m = {
        "recipient_name":   "the RECIPIENT — the person/business this letter is addressed TO (e.g. landlord, employer, supplier).",
        "recipient_address":"the RECIPIENT's postal address (where the letter is sent TO).",
        "sender_name":      "the SENDER — the USER themselves (the person writing the letter). DO NOT extract from the text unless the user is explicitly named as the writer; usually leave this blank.",
        "sender_address":   "the SENDER's postal address (the USER's own address). DO NOT extract unless the text clearly identifies the user's own address.",
        "subject":          "the subject / Betreff line of the letter.",
        "body":             "the body prose of the letter.",
        "greeting":         "the opening salutation (Anrede).",
        "closing":          "the closing phrase.",
        "date":             "a date value (any ISO or German DD.MM.YYYY format).",
        "reference_number": "a contract / customer / invoice number.",
        "currency_amount":  "a monetary value.",
        "location":         "a place name, city, or venue.",
        "freeform_text":    "freeform multi-line prose.",
        "freeform_value":   "a freeform scalar (single value).",
        "line_items":       "a LIST of invoice line items. EMIT AS A JSON ARRAY of objects matching the schema in the hint — NEVER as a scalar string or a concatenated description.",
    }
    return m.get(role, "")


# Position-key suffixes per language. The flat-key shape is
# position_<N>_<suffix>; the LLM-facing target list collapses ALL of
# these into a single synthetic 'positions' field that the model emits
# as a JSON array. render_template's Jinja branch reads either shape.
_POSITION_SUFFIXES_DE = ("_beschreibung", "_menge", "_einheit", "_einzelpreis")
_POSITION_SUFFIXES_EN = ("_description", "_quantity", "_unit", "_unit_price")


def _detect_position_group(specs: List[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """If the template carries position_<N>_* flat keys, return a
    synthetic 'positions' target spec for the LLM-facing prompt.
    Returns None when no line-item keys are present.

    Lets compose_extract_args teach the model to emit a real
    positions=[{...}, {...}, ...] array instead of cramming each item
    into a separate flat key — the 5-item Jinja cap is gone (Fix 8
    sub-round 1), but until extraction knows about the new shape the
    model would still hit the cap from the prompt structure alone.
    """
    has_de = any(
        isinstance(s, dict) and (s.get("key") or "").startswith("position_")
        and any((s.get("key") or "").endswith(suf) for suf in _POSITION_SUFFIXES_DE)
        for s in specs
    )
    has_en = any(
        isinstance(s, dict) and (s.get("key") or "").startswith("position_")
        and any((s.get("key") or "").endswith(suf) for suf in _POSITION_SUFFIXES_EN)
        for s in specs
    )
    if not (has_de or has_en):
        return None
    if has_de:
        return {
            "key":   "positions",
            "label": "Rechnungspositionen (Liste)",
            "role":  "line_items",
            "hint":  (
                "EMIT AS A JSON ARRAY. Each element is an object: "
                "{\"beschreibung\": \"<Bezeichnung der Leistung, ggf. mehrzeilig>\", "
                "\"menge\": <Anzahl als Zahl>, "
                "\"einheit\": \"<Std / Stück / Pauschal / kg / m / Tag / ...>\", "
                "\"einzelpreis\": <Netto-Einzelpreis in Euro als Zahl>}. "
                "Use ONE element per distinct posten the user named. Don't cap at 5. "
                "Example — input 'Beratung 4h à 100€, Anfahrt pauschal 50€, Material 20€': "
                "positions = [{\"beschreibung\":\"Beratung\",\"menge\":4,\"einheit\":\"Std\",\"einzelpreis\":100}, "
                "{\"beschreibung\":\"Anfahrt\",\"menge\":1,\"einheit\":\"Pauschal\",\"einzelpreis\":50}, "
                "{\"beschreibung\":\"Material\",\"menge\":1,\"einheit\":\"Pauschal\",\"einzelpreis\":20}]."
            ),
        }
    return {
        "key":   "positions",
        "label": "Invoice line items (list)",
        "role":  "line_items",
        "hint":  (
            "EMIT AS A JSON ARRAY. Each element is an object: "
            "{\"description\": \"<service or item, may be multi-line>\", "
            "\"quantity\": <number>, "
            "\"unit\": \"<hr / ea / lot / day / kg / m / ...>\", "
            "\"unit_price\": <unit price as a number>}. "
            "Use ONE element per distinct line item the user named. Don't cap at 5. "
            "Example — input 'Consulting 4h @ $100, travel $50 flat, materials $20': "
            "positions = [{\"description\":\"Consulting\",\"quantity\":4,\"unit\":\"hr\",\"unit_price\":100}, "
            "{\"description\":\"Travel\",\"quantity\":1,\"unit\":\"lot\",\"unit_price\":50}, "
            "{\"description\":\"Materials\",\"quantity\":1,\"unit\":\"lot\",\"unit_price\":20}]."
        ),
    }


def _strip_position_flat_keys(specs: List[dict[str, Any]]) -> List[dict[str, Any]]:
    """Drop position_<N>_<suffix> entries from the LLM-facing target
    list. Used in tandem with _detect_position_group — the synthetic
    'positions' entry replaces them so the model isn't shown both
    shapes (which the 9B would otherwise try to fill simultaneously,
    duplicating data and breaking the totals)."""
    all_suffixes = _POSITION_SUFFIXES_DE + _POSITION_SUFFIXES_EN
    out: List[dict[str, Any]] = []
    for s in specs:
        key = (s.get("key") or "") if isinstance(s, dict) else ""
        if key.startswith("position_") and any(key.endswith(suf) for suf in all_suffixes):
            continue
        out.append(s)
    return out


def _build_prompt(text: str, specs: List[dict[str, Any]], contact_group: Optional[str]) -> str:
    """Build the LLM prompt. Each target field carries explicit role
    meaning so similar-spelled keys can't bleed into each other."""
    # Collapse position_<N>_<suffix> flat keys into a single synthetic
    # 'positions' target — the model gets one schema-rich hint instead
    # of N×4 separate per-row keys, and emits a JSON array that maps
    # 1:1 onto the template's new Jinja branch (Fix 8 sub-round 1).
    pos_spec = _detect_position_group(specs)
    if pos_spec is not None:
        specs = _strip_position_flat_keys(specs) + [pos_spec]
    lines: List[str] = []
    lines.append(
        "You extract structured field values from a user-provided text blob. "
        "The text may contain extra/irrelevant content; ignore anything you "
        "can't confidently map to one of the target fields below.\n"
        "\n"
        "CRITICAL: pay attention to the ROLE MEANING — keys can look similar "
        "but represent different sides of the letter. Example: `mieter_name` "
        "(tenant / sender / USER) is NOT the same as `vermieter_name` "
        "(landlord / recipient / addressee). If the text says \"Vermieter: "
        "Hans Müller\", that's the RECIPIENT name, not the sender.\n"
        "\n"
        "SECOND CRITICAL: some target fields carry role=line_items — those "
        "MUST be emitted as a JSON array, not a string. See the field's hint "
        "for the per-row schema."
    )
    if contact_group:
        lines.append(
            f"\nThis extraction is scoped to the '{contact_group}' contact "
            "group only. Don't extract values for any other group; if the "
            "text contains other recipients, IGNORE them."
        )
    lines.append("\n=== TARGET FIELDS ===")
    for s in specs:
        key = s.get("key") or ""
        role = s.get("role") or ""
        label = s.get("label") or key
        hint = s.get("hint") or ""
        bits = [f"  - key: {key}", f"    label: {label}"]
        if role:
            bits.append(f"    role: {role}")
            meaning = _role_meaning(role)
            if meaning:
                bits.append(f"    meaning: {meaning}")
        if hint:
            bits.append(f"    hint: {hint}")
        lines.append("\n".join(bits))
    lines.append(
        "\n=== INSTRUCTIONS ===\n"
        "- Return STRICT JSON: {\"args\": {<key>: <value>, ...}, "
        "\"confidence\": {<key>: \"<explicit|inferred|guessed>\"}}.\n"
        "- <value> is a STRING for most fields. For fields with "
        "role=line_items, <value> is a JSON ARRAY of objects per the "
        "field's hint. Mixed example: "
        "{\"args\": {\"kunde_name\": \"ACME\", \"positions\": "
        "[{\"beschreibung\":\"Beratung\",\"menge\":2,\"einheit\":\"Std\","
        "\"einzelpreis\":100}, {\"beschreibung\":\"Reise\",\"menge\":1,"
        "\"einheit\":\"Pauschal\",\"einzelpreis\":50}]}, "
        "\"confidence\": {\"kunde_name\": \"explicit\", \"positions\": "
        "\"explicit\"}}.\n"
        "- Only include keys where you found the value in the text. "
        "Omit fields you can't confidently fill (do NOT invent values).\n"
        "- READ each field's ROLE MEANING before deciding. A label in the "
        "text identifies the RECIPIENT side (Vermieter, Empfänger, "
        "Arbeitgeber, To:) — those values map to role=recipient_*, NEVER "
        "to role=sender_*. The sender is the user; omit sender fields "
        "unless the user is explicitly named as the writer.\n"
        "- For address-shaped fields, format as multi-line with \\n between "
        "street, postcode + city, country.\n"
        "- For name fields, use the person's or business's display name.\n"
        "- For role=line_items fields, the text usually lists items "
        "separated by commas, semicolons, dashes, or bullet points. "
        "ALWAYS emit ONE array element per item the user named — even "
        "if there are 8 or 10. Parse out the quantity, unit, and price "
        "as separate fields per row; don't cram the whole '4h à 100€' "
        "string into a single beschreibung.\n"
        "- Strip any field labels (\"Vermieter:\", \"Address:\", etc.) "
        "from the values — emit just the data.\n"
        "- Use \"explicit\" confidence when a label in the text identifies "
        "the field, \"inferred\" when format-recognised without a label, "
        "\"guessed\" only when you had to interpret heavily.\n"
        "- NO commentary, NO markdown, NO code fences. Just the JSON."
    )
    lines.append("\n=== USER TEXT ===")
    lines.append(text)
    lines.append("\n=== JSON OUTPUT ===")
    return "\n".join(lines)


def _parse_response(raw: str) -> dict[str, Any]:
    """Tolerant JSON extraction — strips code fences, falls back to
    finding the first {...} block when the model wrapped its output."""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


async def execute(
    ctx,
    text: str,
    template_id: str,
    contact_group: Optional[str] = None,
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {
            "ok":        False,
            "args":      {},
            "_llm_hint": "Extract requested but text was empty — ask the user to paste content.",
        }
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]

    from backend.compose import templates as _tpl_mod
    try:
        template = _tpl_mod.get(template_id)
    except Exception:
        return {
            "ok":        False,
            "args":      {},
            "_llm_hint": (
                f"UNKNOWN_TEMPLATE_ID: {template_id!r}. Call "
                "list_compose_templates to see the canonical ids, "
                "then retry."
            ),
        }

    specs = _scoped_arg_specs(template, contact_group)
    if not specs:
        return {
            "ok":        False,
            "args":      {},
            "_llm_hint": (
                f"Template {template_id!r} has no ask_user_for_args "
                + (f"for contact_group={contact_group!r}" if contact_group else "declarations")
                + ". Add role declarations (see templates/SCHEMA.md) before "
                "extraction can target specific slots."
            ),
        }

    from backend.agent.llm import LlmClient
    client = LlmClient(
        model=os.getenv("HOMEOS_MODEL", "qwen3.6-27b-mtp"),
        base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
    )
    prompt = _build_prompt(text, specs, contact_group)

    try:
        resp = await asyncio.to_thread(
            client.chat,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("compose_extract_args LLM call failed: %s", exc)
        return {
            "ok":        False,
            "args":      {},
            "_llm_hint": f"Extraction LLM call failed: {exc}. User can type the values manually.",
        }

    parsed = _parse_response(resp.get("content") or "")
    args_out = parsed.get("args") if isinstance(parsed, dict) else None
    confidence = parsed.get("confidence") if isinstance(parsed, dict) else None
    if not isinstance(args_out, dict):
        args_out = {}
    if not isinstance(confidence, dict):
        confidence = {}

    # Filter to ONLY the target arg keys (defensive — the model might
    # hallucinate keys not in the schema). Include the synthetic
    # 'positions' key whenever the prompt collapsed flat position_<N>_*
    # entries into it, so the array value the LLM emits actually
    # survives instead of being dropped by the "isinstance str" check.
    target_keys = {s.get("key") for s in specs if isinstance(s, dict)}
    if _detect_position_group(specs) is not None:
        target_keys.add("positions")
    clean_args: dict[str, Any] = {}
    for k, v in args_out.items():
        if k not in target_keys:
            continue
        if isinstance(v, str):
            if v.strip():
                clean_args[k] = v
        elif isinstance(v, list):
            # role=line_items emits a JSON array. Keep non-empty lists
            # whose items are dicts; otherwise drop (a list of strings
            # is the model getting the schema half-right and isn't
            # safe to merge into the template).
            kept = [item for item in v if isinstance(item, dict) and item]
            if kept:
                clean_args[k] = kept
    clean_conf = {k: v for k, v in confidence.items() if k in clean_args}

    n = len(clean_args)
    if n == 0:
        hint = (
            "No fields could be extracted from the pasted text. The text "
            "may be too noisy / unrelated, or it described different "
            "fields than this template needs. Ask the user to type the "
            "values directly into the args panel."
        )
    else:
        hint = (
            f"Extracted {n} field(s)"
            + (f" for contact_group={contact_group!r}" if contact_group else "")
            + f": {sorted(clean_args.keys())}. "
            "Apply these to the draft (compose_draft args=…). Fields with "
            "confidence='inferred' or 'guessed' should be surfaced to the "
            "user for a quick verify before send."
        )

    return {
        "ok":         True,
        "args":       clean_args,
        "confidence": clean_conf,
        "_llm_hint":  hint,
    }
