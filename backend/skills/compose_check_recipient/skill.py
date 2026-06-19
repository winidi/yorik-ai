"""compose_check_recipient — decide whether a contact has the address data
needed to render a postal letter template, or whether we need to collect it.

This is the deterministic seam between find_contact (does the person exist?)
and compose_draft (render the letter). It exists so the LLM follows one
fixed playbook instead of guessing whether to ask the user, mine
Paperless (Phase 3), or just draft.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional


# Per-conversation loop guard. Same (contact_id, template_id) pair gets
# at most _LOOP_GUARD_LIMIT calls before the skill refuses with a
# routing hint. The chat audit caught a 9-call loop where the LLM kept
# retrying with the same args + wrong template id; this stops that pattern.
_LOOP_GUARD_LIMIT = 3
_call_counts: ContextVar[dict[tuple[int, str], int]] = ContextVar(
    "compose_check_recipient_calls", default=None  # type: ignore[arg-type]
)


def _bump_and_check(cid: int, tid: str) -> int:
    """Increment the per-turn counter for this (contact_id, template_id)
    pair, return the new count. Safe to call when the ContextVar isn't
    initialised — it lazy-creates a fresh dict."""
    counts = _call_counts.get()
    if counts is None:
        counts = {}
        _call_counts.set(counts)
    key = (cid, (tid or "").strip())
    n = counts.get(key, 0) + 1
    counts[key] = n
    return n


# Address-kind preference order — home addresses win for personal letters.
# Falls through to work / billing / shipping / other in that order.
_ADDR_PRIORITY = {"home": 0, "work": 1, "billing": 2, "shipping": 3}


def _pick_best_address(addrs: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not addrs:
        return None
    return sorted(
        addrs,
        key=lambda a: _ADDR_PRIORITY.get((a.get("kind") or "").lower(), 9),
    )[0]


def _template_is_localized(template: dict, lang_prefix: str) -> bool:
    """Best-effort: does the template id / tags / vertical look like the
    given language? Used to label form fields in the user's language."""
    tid = (template.get("id") or "").lower()
    tags = [t.lower() for t in (template.get("tags") or [])]
    return tid.endswith(f"-{lang_prefix}") or lang_prefix in tags


def _field_labels(template: dict) -> dict[str, str]:
    if _template_is_localized(template, "de"):
        return {
            "line1":    "Straße + Hausnr.",
            "line2":    "Adresszusatz (optional)",
            "postcode": "PLZ",
            "city":     "Ort",
            "country":  "Land (optional)",
        }
    if _template_is_localized(template, "it"):
        return {
            "line1":    "Via e numero civico",
            "line2":    "Complemento (opzionale)",
            "postcode": "CAP",
            "city":     "Città",
            "country":  "Paese (opzionale)",
        }
    if _template_is_localized(template, "fr"):
        return {
            "line1":    "Rue et numéro",
            "line2":    "Complément (facultatif)",
            "postcode": "Code postal",
            "city":     "Ville",
            "country":  "Pays (facultatif)",
        }
    # default EN
    return {
        "line1":    "Street + Number",
        "line2":    "Address line 2 (optional)",
        "postcode": "ZIP / Postcode",
        "city":     "City",
        "country":  "Country (optional)",
    }


async def execute(
    ctx,
    contact_id: int,
    template_id: str,
) -> dict[str, Any]:
    from backend import contacts as C
    from backend.compose import templates as T

    try:
        cid = int(contact_id)
    except (TypeError, ValueError):
        return {
            "_llm_hint": f"contact_id must be an integer; got {contact_id!r}.",
            "found": False,
        }

    # Loop guard: if the SAME (contact_id, template_id) has been checked
    # too many times this turn, the LLM is stuck. The chat audit caught
    # 9 identical calls before iteration cap. Bail with explicit routing.
    n = _bump_and_check(cid, template_id)
    if n > _LOOP_GUARD_LIMIT:
        return {
            "ok":        False,
            "found":     False,
            "error":     "loop_guard",
            "_llm_hint": (
                f"REFUSED: compose_check_recipient has been called {n} times "
                f"this turn for contact_id={cid}, template_id={template_id!r}. "
                "The answer isn't changing. STOP retrying. Likely causes:\n"
                "  (1) Wrong template_id — call list_compose_templates "
                "to see real ids, then retry compose_draft with the chosen id.\n"
                "  (2) Missing address fields — ask the user for them in plain "
                "language ('Welche Adresse für die Wohnung?') instead of "
                "polling this skill again.\n"
                "  (3) The contact genuinely has no postal address — offer to "
                "add one via add_contact_address, or proceed without (the draft "
                "will surface missing fields in the inline form)."
            ),
        }

    contact = C.get(cid)
    if not contact:
        return {
            "_llm_hint": (
                f"contact_id={cid} not found. Call find_contact again with a "
                "narrower query, or list_contacts_for_picking to scan the "
                "full address book."
            ),
            "found": False,
        }

    try:
        template = T.get(template_id)
    except Exception:
        return {
            "_llm_hint": (
                f"template_id={template_id!r} not found. The Compose context "
                "line carries the active template id — use that exactly."
            ),
            "found": False,
            "contact_id": cid,
            "contact_name": contact["display_name"],
        }

    addrs = contact.get("addresses") or []
    picked = _pick_best_address(addrs)

    name = contact["display_name"]
    labels = _field_labels(template)

    if picked and (picked.get("line1") or "").strip() and (picked.get("city") or "").strip():
        # Have enough to render — return present payload + green-light.
        present = {
            "line1":    (picked.get("line1") or "").strip(),
            "line2":    (picked.get("line2") or "").strip(),
            "postcode": (picked.get("postcode") or "").strip(),
            "city":     (picked.get("city") or "").strip(),
            "country":  (picked.get("country") or "").strip(),
            "kind":     picked.get("kind") or "home",
        }
        pretty = present["line1"]
        if present["postcode"] or present["city"]:
            pretty += ", " + " ".join(filter(None, [present["postcode"], present["city"]]))
        # Tone hint — proximate, this-turn instruction so the LLM doesn't
        # forget the rule that lives in compose_draft's args.description.
        # When the recipient is a person we know by first name, suggest
        # the informal anrede + gruss override; the LLM still decides
        # (e.g. won't override on a Kündigung or Sie-form body).
        first_name = (contact.get("first_name") or "").strip()
        if not first_name and contact.get("kind") == "person":
            # Fall back to the first whitespace-token of display_name.
            first_name = (contact.get("display_name") or "").split(" ")[0]
        tone_block = ""
        if contact.get("kind") == "person" and first_name:
            tone_block = (
                f"\n\nTONE HINT: {name} is a person on first-name basis "
                f"(first_name={first_name!r}). If you are writing a CASUAL "
                "letter (du-form body, or the user said 'Freund'/'Oma'/"
                "'Bruder' etc.), PASS args={"
                f'"anrede": "Hallo {first_name}," or "Liebe {first_name},", '
                f'"gruss": "Liebe Grüße" or "Viele Grüße"'
                "} to compose_draft so the formal template defaults don't "
                "wrap your informal body. Skip the override for formal "
                "letters (business / authority / Kündigung / body in Sie-form)."
            )
        return {
            "_llm_hint": (
                f"OK: {name} has a postal address on file ({present['kind']}): "
                f"{pretty}. PROCEED to compose_draft now with "
                f"contact_id={cid}, template_id={template_id!r}. "
                "Do NOT ask the user to confirm the address — it's already "
                "the canonical one. The Compose UI lets them edit if needed."
                + tone_block
            ),
            "complete":      True,
            "contact_id":    cid,
            "contact_name":  name,
            "template_id":   template_id,
            "address":       present,
        }

    # Incomplete — list what's missing in the user's language.
    if picked:
        # Partial address — collect only the missing pieces.
        missing = []
        if not (picked.get("line1") or "").strip():
            missing.append({"key": "line1", "label": labels["line1"], "required": True})
        if not (picked.get("city") or "").strip():
            missing.append({"key": "city", "label": labels["city"], "required": True})
        if not (picked.get("postcode") or "").strip():
            missing.append({"key": "postcode", "label": labels["postcode"], "required": False})
        partial = {
            "line1":    (picked.get("line1") or "").strip(),
            "line2":    (picked.get("line2") or "").strip(),
            "postcode": (picked.get("postcode") or "").strip(),
            "city":     (picked.get("city") or "").strip(),
            "country":  (picked.get("country") or "").strip(),
            "kind":     picked.get("kind") or "home",
        }
    else:
        # No address at all — need the full set.
        missing = [
            {"key": "line1",    "label": labels["line1"],    "required": True},
            {"key": "postcode", "label": labels["postcode"], "required": False},
            {"key": "city",     "label": labels["city"],     "required": True},
        ]
        partial = None

    # Per Phase 3 playbook the LLM should call find_recipient_address_from_documents
    # next, not ask the user directly. Surface that as the deterministic next step
    # in the hint so qwen doesn't fall back to prose questions.
    hint = (
        f"{name} has NO usable postal address on file "
        f"(missing: {', '.join(m['key'] for m in missing)}).\n"
        "MANDATORY NEXT STEP — do NOT ask the user yet:\n"
        f"  CALL find_recipient_address_from_documents(contact_id={cid}). "
        "It mines past Paperless documents for addresses tied to this contact "
        "AND emits the inline address form the user fills in. The returned "
        "hint will tell you whether to (a) present a found address to the "
        "user for confirmation, or (b) ask the user directly (only when "
        "Paperless turned up nothing).\n"
        "Do NOT call compose_draft now — without an address the recipient "
        "block renders empty and the user has to fix it."
    )

    # NB: we used to emit a needs_input card here too, on the theory the
    # user could start typing while Paperless was being mined in parallel.
    # In practice the LLM runs the two skills sequentially, so both cards
    # landed back-to-back in the chat ("Postanschrift von X" stacked
    # twice). find_recipient_address_from_documents emits the canonical
    # card with richer context (mined candidates as quick-fill chips,
    # or a "nothing in your docs either" hint when empty), so we let it
    # be the single source of truth and stay quiet here.

    return {
        "_llm_hint":      hint,
        "complete":       False,
        "contact_id":     cid,
        "contact_name":   name,
        "template_id":    template_id,
        "address":        partial,
        "missing":        missing,
        "next_step":      "mine_paperless",
    }
