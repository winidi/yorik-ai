"""compose_check_template_args — find template arg slots that nobody has
filled in yet AND that we can't auto-fill from contact/profile, then ask
the user via an inline form.

This is the seam between compose_check_recipient (which checks the
recipient address) and compose_draft (which renders). Without it the
LLM ends up making up dates / contract numbers / apartment addresses
because it has no mechanism to ASK for them — and the template either
renders awkwardly with empty slots or with hallucinated values.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Regex over body_html that finds every distinct `{{ args.X }}` arg key.
_ARG_REF_RE = re.compile(r"\{\{\s*args\.([a-zA-Z_][a-zA-Z0-9_]*)")

# Slots compose_draft auto-fills from contact / profile / today() —
# scanning for these in a template doesn't mean we have to ask the user.
# Kept as a set of (lowercased) key names that match exactly or by suffix.
_AUTO_FILLED_EXACT: set[str] = {
    # Recipient (filled from contact_id)
    "recipient", "recipient_name", "recipient_address", "recipient_address_line1",
    "empfaenger_name", "empfaenger_adresse",
    "vermieter_name", "vermieter_adresse",
    "anbieter_name", "anbieter_adresse",
    "locatore_nome", "locatore_indirizzo",
    "conduttore_indirizzo",
    "sprzedawca_nazwa", "sprzedawca_adres",
    "wynajmujacy_imie_nazwisko", "wynajmujacy_adres",
    # Sender (filled from user profile)
    "sender_name", "sender_first_name", "sender_last_name",
    "sender_address", "sender_phone", "sender_business_name",
    "absender_name", "absender_adresse",
    "mieter_name", "mieter_adresse",
    "konduttore_nome", "konduttore_indirizzo",
    "konsument_imie_nazwisko", "konsument_adres",
    "wierzyciel_nazwa", "wierzyciel_adres",
    # Auto-derived
    "ort",                       # user.address_city
    "signature_image_url",       # user.signature_data_url
    "unterschrift_url",
    "subject",                   # user-supplied or template-default
    # Tax id / IBAN — filled from user.tax_id / user.iban via the sender
    # auto-fill block in compose_draft. Listed here so the form preflight
    # doesn't mark them as "missing" when the profile already has them
    # (e.g. rechnung-de's now-required absender_steuernr).
    "absender_steuernr", "absender_ustid",
    "sender_tax_id", "sender_tax_number",
    "absender_iban", "sender_iban",
}

# Friendly human label per known key. Auto-detected labels fall back to
# a humanized version of the key name ("mietvertrag_vom" → "Mietvertrag vom").
_DEFAULT_LABELS: dict[str, str] = {
    "mietvertrag_vom":         "Mietvertrag vom (Datum, z.B. 15.06.2022)",
    "beendigung_zum":          "Beendigung zum (Datum, z.B. 31.03.2027)",
    "kuendigung_zum":          "Kündigung zum (Datum oder „nächstmöglichen Termin\")",
    "wohnung_adresse":         "Wohnung (Straße, Etage, PLZ Ort)",
    "neue_adresse":            "Neue Adresse für die Endabrechnung (optional)",
    "uebergabe_terminvorschlag": "Terminvorschlag für die Wohnungsübergabe",
    "kunden_oder_vertragsnummer": "Kunden-/Vertragsnummer",
    "vertragsart":             "Vertragsart (z.B. „Fitnessstudio-Mitgliedschaft\")",
    "vertragsbeginn":          "Vertragsbeginn (Datum)",
    "kontoinhaber_iban":       "IBAN (für SEPA-Lastschrift-Widerruf)",
    "kuendigungsgrund_satz":   "Begründung (optional, 1 Satz)",
    "betreff":                 "Betreff",
    "anrede":                  "Anrede (z.B. „Sehr geehrte Damen und Herren,\")",
    "gruss":                   "Grußformel (z.B. „Mit freundlichen Grüßen\")",
    "body_text":               "Brieftext (das, was du sagen möchtest)",
    "brief_text":              "Brieftext",
    "letter_text":             "Letter body text",
}


def _humanize(key: str) -> str:
    """Fallback label for unknown arg keys. snake_case → Title Case."""
    if key in _DEFAULT_LABELS:
        return _DEFAULT_LABELS[key]
    return key.replace("_", " ").strip().capitalize()


def _scan_template_arg_keys(template: dict[str, Any]) -> list[str]:
    """Return the args.X keys referenced by template.body_html, deduped,
    preserving first-seen order so the form shows them top-to-bottom."""
    body = template.get("body_html") or ""
    seen: dict[str, None] = {}
    for m in _ARG_REF_RE.finditer(body):
        seen.setdefault(m.group(1), None)
    return list(seen.keys())


def _role_auto_filled_keys(
    template: dict[str, Any], contact_id: Optional[int]
) -> set[str]:
    """Honor the template's `role` metadata to identify keys that
    compose_draft will fill automatically — so this skill's preflight
    doesn't blindly add them to the missing list.

    Without this, templates that name their recipient slots in a
    non-canonical pattern (rechnung-de's `kunde_*`, invoice-en's
    `customer_*`) tripped the form-preflight even when the caller
    passed contact_id, because the old code only knew about a
    hardcoded prefix list (`recipient_`, `empfaenger_`, `vermieter_`,
    etc.). Reading the role declaration the author already wrote is
    template-agnostic and avoids the next "I named my key X and
    the form keeps asking for it" bug.

    recipient_* roles only count as auto-filled when contact_id is
    provided. sender_* roles are always profile-driven, so they're
    auto-filled unconditionally.
    """
    out: set[str] = set()
    asks = template.get("ask_user_for_args") or []
    if not isinstance(asks, list):
        return out
    _RECIPIENT_ROLES = {
        "recipient_name", "recipient_address",
        "recipient_email", "recipient_phone",
    }
    _SENDER_ROLES = {
        "sender_name", "sender_address",
        "sender_email", "sender_phone",
    }
    for entry in asks:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        role = entry.get("role")
        if not key or not role:
            continue
        if role in _SENDER_ROLES:
            out.add(key)
        elif contact_id and role in _RECIPIENT_ROLES:
            out.add(key)
    return out


def _is_truly_empty(args: dict[str, Any], key: str) -> bool:
    v = args.get(key)
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


async def execute(
    ctx,
    template_id: str,
    contact_id: Optional[int] = None,
    args: Optional[dict[str, Any]] = None,
    existing_draft_id: Optional[int] = None,
) -> dict[str, Any]:
    from backend.compose import templates as T
    from backend.ui_tools import _append, get_ui_actions as _get_pending_ui

    # ── Picker-wait guard: same contract as compose_draft. If
    # pick_compose_template emitted a template_picker this turn, the
    # user hasn't clicked yet — calling this skill now would surface a
    # needs_input form on top of the picker. Skip for existing_draft_id
    # (locked draft, no picker involved).
    if not existing_draft_id:
        try:
            for _act in (_get_pending_ui() or []):
                if isinstance(_act, dict) and _act.get("type") == "template_picker":
                    return {
                        "complete": False,
                        "missing":  [],
                        "_llm_hint": (
                            "PICKER_PENDING: pick_compose_template just "
                            "rendered a template_picker card this turn. "
                            "STOP — wait for the user to click and send a "
                            "`[template_picked id=X]` follow-up before "
                            "calling compose_check_template_args (or "
                            "compose_draft). Reply ONE short sentence "
                            "asking the user to pick; do NOT call "
                            "compose_check_template_args again this turn."
                        ),
                    }
        except Exception:
            pass

    try:
        template = T.get(template_id)
    except Exception:
        return {
            "_llm_hint": (
                f"template_id={template_id!r} not found. The Compose context "
                "line carries the active template id — use that exactly."
            ),
            "complete": False,
            "missing": [],
        }

    args = dict(args or {})

    # ── Drop keys the template doesn't recognize ───────────────────────
    # The 9B model regularly hallucinates "generic invoice" arg names —
    # betrag_netto, betrag_mwst, datum, waehrung — instead of the
    # template's actual keys (position_1_einzelpreis, mwst_prozent,
    # rechnungsdatum). Those bogus keys stick around in the args dict,
    # so the Bug 4 extraction gate (which counts non-empty values to
    # decide whether to skip) gets tricked into thinking we have plenty
    # of args and extraction stays skipped. The real values from the
    # user's intent never reach the right keys; the rendered invoice
    # shows €0,00 even though the user said €380.
    #
    # Compute the template's "known" keys (anything referenced by body
    # plus any default_args declaration) and drop everything else
    # before any downstream logic looks at args. Safe because:
    #   - referenced keys are exactly what the renderer expands
    #   - default_args keys are what the template author declared as
    #     valid slots (even if empty)
    #   - dropping unknown keys at this seam means the LLM's invented
    #     keys can't poison form-emission or merge order
    try:
        _known: set[str] = set()
        for k in _scan_template_arg_keys(template):
            _known.add(k)
        for k in (template.get("default_args") or {}):
            _known.add(k)
        for k in _AUTO_FILLED_EXACT:
            _known.add(k)
        if _known:
            args = {k: v for k, v in args.items() if k in _known}
    except Exception:
        pass  # never block form emission on a key-filter hiccup

    # When the caller is iterating on an existing draft (the common case
    # after the first compose_draft turn), pull the draft's persisted
    # args first so fields the user / LLM already filled — especially
    # body_text from a prior chat turn — aren't re-asked in the form.
    # Without this, body intent leaks through the form-submit round-trip
    # and the rendered PDF keeps showing the placeholder.
    if existing_draft_id:
        try:
            import json as _json_load
            from backend.database import conn_ctx as _cctx_pre, DEFAULT_DB_PATH as _ddb_pre
            with _cctx_pre(_ddb_pre) as _c:
                _row = _c.execute(
                    "SELECT args_json FROM compose_drafts WHERE id = ?",
                    (int(existing_draft_id),),
                ).fetchone()
            if _row and _row["args_json"]:
                stored = _json_load.loads(_row["args_json"]) or {}
                for k, v in stored.items():
                    if _is_truly_empty(args, k) and not _is_truly_empty(stored, k):
                        args[k] = v
        except Exception:
            pass  # never block the form on a corrupt args_json

    # ── Auto-extract from conversation history (Bug 4 fix) ─────────────
    # The 9B model regularly forgets to pass args={...} to this skill
    # even when the user already stated values in their original message
    # ("kündige meinen Vertrag zum 31.12.2026, Policennummer L-12345").
    # Before emitting the form, pull the recent user messages and run
    # them through compose_extract_args server-side. Any values the
    # LLM extracts get merged into args — the form then only asks for
    # what's truly missing. Skipped on refines (existing_draft_id) since
    # the stored draft already covers prior args, and on calls where
    # args is already substantial (caller did the job).
    # Gate: run extraction whenever the template still has a required
    # field that's empty after defaults/auto-fill. The old `< 3 filled`
    # heuristic was wrong for partial form_submit turns — once the user
    # answered 3 minimum fields (kunde_*, leistung_kurz), the gate
    # tripped and we'd skip extracting position_1_einzelpreis from the
    # original "über 420 Euro" intent, leaving the draft at €0.
    _ask = template.get("ask_user_for_args") or []
    _required_keys = [
        f.get("key") for f in _ask
        if isinstance(f, dict) and f.get("required") and f.get("key")
    ]
    _any_required_missing = any(_is_truly_empty(args, k) for k in _required_keys)
    extraction_attempted = False
    if (
        ctx is not None
        and not existing_draft_id
        and _any_required_missing
    ):
        try:
            conv_id = getattr(ctx, "conversation_id", None)
            role = getattr(ctx, "role", "admin")
            if conv_id:
                from backend.agent.conversation_io import load_messages as _load_msgs
                # limit=300 absorbs the post-Phase-4 read-first overhead
                # (each invoke pairs with a skill_view, doubling message
                # count per turn) so the original "schreib X" intent is
                # still reachable even after several picker / form-submit
                # round-trips. At limit=40 a typical 3-turn Rechnung flow
                # already buried the user's intent under tool-result rows
                # and only the synthetic [template_picked] follow-up (which
                # we skip) remained in window — extraction got nothing.
                prior = _load_msgs(conv_id, role, limit=300) or []
                # Grab last ~3 user messages — that's where the intent
                # lives. Skip [form_submit] / [template_picked] /
                # [photo_picked] meta-messages and synthetic system text.
                user_texts: list[str] = []
                for m in reversed(prior):
                    if not isinstance(m, dict): continue
                    if m.get("role") != "user": continue
                    c = m.get("content")
                    if not isinstance(c, str): continue
                    cs = c.strip()
                    if not cs: continue
                    if cs.startswith("["): continue  # synthetic followups
                    user_texts.append(cs)
                    if len(user_texts) >= 3: break
                if user_texts:
                    blob = "\n\n".join(reversed(user_texts))
                    try:
                        extracted = await ctx.call_skill(
                            "compose_extract_args",
                            text=blob,
                            template_id=template_id,
                        )
                        extraction_attempted = True
                        if isinstance(extracted, dict):
                            ex_args = extracted.get("args") or {}
                            if isinstance(ex_args, dict):
                                for k, v in ex_args.items():
                                    if _is_truly_empty(args, k) and v not in (None, "", []):
                                        args[k] = v
                    except Exception:
                        pass  # extraction is best-effort
        except Exception:
            pass  # any failure here must not block the form path

    # Treat the template's own non-empty default_args as "already filled" —
    # the author chose those as sensible starting values.
    template_defaults = template.get("default_args") or {}
    for k, v in template_defaults.items():
        if isinstance(v, str) and v.strip() and _is_truly_empty(args, k):
            args[k] = v

    # Cross-field defaults — small set of well-known same-template fallbacks
    # the law / convention permits. Keeps the form short when the user gave
    # us enough.
    #
    # rechnungsdatum / invoice_date ← today: the renderer's body_html
    # already falls back to today_de() when args.rechnungsdatum is empty
    # ("Datum: {{ args.rechnungsdatum or today_de() }}"). Mirror that
    # default in the preflight so the chat doesn't insist on a date
    # field for the typical "schreib eine Rechnung" intent where today
    # IS the issue date. Templates that genuinely want a different
    # date can still pass it explicitly via extraction or form.
    _referenced_keys = set(_scan_template_arg_keys(template))
    if "rechnungsdatum" in _referenced_keys and _is_truly_empty(args, "rechnungsdatum"):
        from datetime import datetime as _dt
        args["rechnungsdatum"] = _dt.now().strftime("%d.%m.%Y")
    if "invoice_date" in _referenced_keys and _is_truly_empty(args, "invoice_date"):
        from datetime import datetime as _dt
        args["invoice_date"] = _dt.now().strftime("%B %d, %Y")

    # leistungsdatum_von ← rechnungsdatum: §14 (4) 6 UStG allows the
    # Leistungsdatum to equal the Ausstellungsdatum for same-day services
    # ("Leistung am Tag der Rechnungsstellung"). When the user only stated
    # an invoice date, default the service date to match instead of
    # blocking the form on a field they'd just retype. Now also fires
    # when rechnungsdatum was just defaulted to today above — so a
    # one-line "schreib Rechnung" turns into rechnungsdatum=today AND
    # leistungsdatum_von=today without any user input.
    if (
        _is_truly_empty(args, "leistungsdatum_von")
        and not _is_truly_empty(args, "rechnungsdatum")
    ):
        args["leistungsdatum_von"] = args["rechnungsdatum"]
    # Mirror for the invoice-en template (same legal logic, English keys).
    if (
        _is_truly_empty(args, "service_date_from")
        and not _is_truly_empty(args, "invoice_date")
    ):
        args["service_date_from"] = args["invoice_date"]

    # Template-author-declared roles — read once, used in both branches
    # below. Honors the schema instead of guessing from key prefixes.
    _role_filled = _role_auto_filled_keys(template, contact_id)

    # Auto-detect: every referenced arg key not in the auto-fill set,
    # not in args, becomes a missing field.
    referenced = _scan_template_arg_keys(template)
    auto_missing: list[str] = []
    for key in referenced:
        if key in _AUTO_FILLED_EXACT:
            continue
        if key in _role_filled:
            continue
        if not _is_truly_empty(args, key):
            continue
        # Skip recipient-block keys when we know the contact — compose_draft
        # will fill them. Kept as a fallback for templates that lack a
        # role= declaration on their recipient slots (community templates
        # pre-dating the role schema).
        if contact_id and key.startswith(("recipient_", "empfaenger_",
                                            "vermieter_", "anbieter_",
                                            "locatore_", "sprzedawca_",
                                            "wynajmujacy_")):
            continue
        auto_missing.append(key)

    # Per-template polish: ask_user_for_args overrides the auto-detected
    # list with prettier labels and required flags. When set, we use ONLY
    # those keys (filtered to ones still missing).
    explicit_spec = template.get("ask_user_for_args")
    # Track which missing fields are intent-derived ("body" /
    # "freeform_text" roles or an explicit `from_intent: true`). When any
    # exist we ask the LLM to pull from the user's original message
    # BEFORE the form is shown — the user already said it once, no need
    # to retype. Each template controls which of its fields qualify via
    # the `role` / `from_intent` metadata on ask_user_for_args.
    _INTENT_ROLES = {"body", "freeform_text", "subject"}
    # positions[] coalescence: when the caller already supplied a real
    # positions=[{...}, ...] array (extraction emits this, sub-round 3),
    # treat every position_<N>_<suffix> spec entry as satisfied. The
    # template's Jinja renders from args.positions directly, so insisting
    # on flat keys would block the form for fields the renderer doesn't
    # actually need.
    _pos_value = args.get("positions") if isinstance(args, dict) else None
    _has_positions_array = (
        isinstance(_pos_value, list)
        and _pos_value
        and any(isinstance(item, dict) and item for item in _pos_value)
    )
    _POSITION_SUFFIXES = (
        "_beschreibung", "_description",
        "_menge", "_quantity",
        "_einheit", "_unit",
        "_einzelpreis", "_unit_price",
    )

    def _is_position_flat_key(k: str) -> bool:
        return k.startswith("position_") and any(k.endswith(s) for s in _POSITION_SUFFIXES)

    intent_missing: list[dict[str, Any]] = []
    if isinstance(explicit_spec, list) and explicit_spec:
        spec_by_key = {entry["key"]: entry for entry in explicit_spec
                        if isinstance(entry, dict) and "key" in entry}
        fields = []
        for entry in explicit_spec:
            key = entry.get("key")
            if not key:
                continue
            if not _is_truly_empty(args, key):
                continue
            if _has_positions_array and _is_position_flat_key(key):
                continue
            # role=line_items fields can't be filled via the chat needs_input
            # form — they're dynamic-list-shaped (array of objects), not a
            # single value. Sub-round 2's editor lives in the Compose app's
            # args panel; chat extraction (sub-round 3) handles the
            # natural-language path. Skip them here so the chat form
            # never tries to render a text input for an array-shaped key.
            if entry.get("role") == "line_items":
                continue
            # Role-declared auto-fill: the template author marked this
            # slot as a recipient_* or sender_* role, and compose_draft
            # will populate it (recipient_* only when contact_id is set,
            # sender_* always from profile). This is the path that
            # rescues rechnung-de's `kunde_*` and invoice-en's
            # `customer_*` keys, which never matched the
            # `_AUTO_FILLED_EXACT` set or the legacy prefix list.
            if key in _role_filled:
                continue
            # Legacy fallback for templates without role declarations:
            # if the entry is in the auto-fill set, decide whether the
            # skill can actually fill it for THIS call:
            #   - sender_* / today / ort  → always auto-filled from user
            #     profile and date, so skip
            #   - recipient/empfaenger/vermieter/etc. → only auto-filled
            #     when contact_id is given. Without a contact the only
            #     way to get a recipient block is to ASK the user.
            if key in _AUTO_FILLED_EXACT:
                _is_recipient_key = key.startswith((
                    "recipient", "empfaenger_",
                    "vermieter_", "anbieter_",
                    "locatore_", "conduttore_",
                    "sprzedawca_", "wynajmujacy_",
                ))
                if not _is_recipient_key or contact_id:
                    continue
            is_intent = (
                entry.get("role") in _INTENT_ROLES
                or bool(entry.get("from_intent"))
            )
            if is_intent:
                intent_missing.append({
                    "key":       key,
                    "label":     entry.get("label") or _humanize(key),
                    "hint":      entry.get("hint"),
                    # llm_hint is template-author-supplied writing rules
                    # for this field (tone, paragraph breaks, what to
                    # avoid, etc.). Surfaced verbatim to the LLM so each
                    # template controls its own voice instead of the
                    # skill carrying generic rules that fight per-template
                    # nuance. friend-letter and Mietkündigung want very
                    # different things in the same body_text slot.
                    "llm_hint":  entry.get("llm_hint"),
                })
            fields.append({
                "key":      key,
                "label":    entry.get("label") or _humanize(key),
                "required": bool(entry.get("required", False)),
                "value":    "",
                "pattern":  entry.get("pattern"),
                "hint":     entry.get("hint"),
                # "textarea" makes the form render a multi-line input —
                # essential for body_text / freitext slots.
                "input":    entry.get("input"),
                # role / from_intent surface so the frontend can render
                # the "Yorik formuliert für mich" sparkle button on
                # intent-derived fields. Same enum the
                # compose_polish endpoint accepts.
                "role":        entry.get("role"),
                "from_intent": bool(entry.get("from_intent")) or None,
            })
    else:
        # Auto-detected fields with humanized labels. Required defaults
        # to False — the user can skip fields they don't have, and the
        # template's {% if %} guards handle empty slots gracefully.
        # Auto-detected body_text-shaped keys get textarea rendering AND
        # the role="body" flag so the frontend sparkle button renders
        # for templates that didn't author ask_user_for_args metadata.
        _LONG_KEYS = {"body_text", "freitext", "text", "kuendigungsgrund_satz",
                      "notes", "anmerkungen"}
        fields = [
            {"key": k, "label": _humanize(k), "required": False, "value": "",
             "pattern": None, "hint": None,
             "input": "textarea" if k in _LONG_KEYS else None,
             "role": "body" if k in _LONG_KEYS else None,
             "from_intent": None}
            for k in auto_missing
        ]

    # Strip pattern/hint/input/role/from_intent when None so the form
    # payload stays tight (frontend treats absence + None identically).
    for f in fields:
        if f.get("pattern") is None: f.pop("pattern", None)
        if f.get("hint") is None: f.pop("hint", None)
        if f.get("input") is None: f.pop("input", None)
        if f.get("role") is None: f.pop("role", None)
        if f.get("from_intent") is None: f.pop("from_intent", None)

    if not fields:
        return {
            "_llm_hint": (
                f"OK: all template args for {template.get('name', template_id)} "
                "are filled (or auto-fillable). PROCEED to compose_draft now."
            ),
            "complete":     True,
            "template_id":  template_id,
            "missing":      [],
            # Expose the augmented args so compose_draft (the only
            # caller) merges them into its own dict. Without this the
            # skill's Bug 4 extraction / leistungsdatum_von default /
            # tax_id auto-fill work happens in local scope and is
            # thrown away — the LLM's stale guess wins downstream and
            # the rendered draft shows €0,00 even though we DID
            # extract the user-stated €420.
            "args":         args,
        }

    # If only OPTIONAL fields are missing, complete=true. We don't block
    # the playbook for optionals (e.g. Mietvertrag-Datum, Endabrechnungs-
    # adresse) — they're discoverable + editable in the Compose editor's
    # args panel anyway. Blocking would mean either (a) nagging the user
    # for things they don't need OR (b) looping the form after they
    # submitted with empty optionals.
    required_fields = [f for f in fields if f.get("required")]
    if not required_fields:
        opt_keys = ", ".join(f["key"] for f in fields)
        return {
            "_llm_hint": (
                f"OK: all REQUIRED template args are filled. Only optional "
                f"fields are still empty ({opt_keys}) — the user can fill "
                "them in the Compose editor's args panel if they want. "
                "PROCEED to compose_draft now."
            ),
            "complete":     True,
            "template_id":  template_id,
            "missing":      [],
            "optional_unfilled": [f["key"] for f in fields],
            # Same rationale as the all-filled branch above — expose the
            # augmented args so compose_draft's preflight propagates them
            # into the saved draft instead of discarding them.
            "args":         args,
        }

    # Required missing → emit form with REQUIRED + OPTIONAL fields so
    # the user gets the full picture in one place, but only required
    # fields validate. The form submit always resumes the playbook.
    fields_for_form = fields

    # Emit needs_input so ComposeAgentChat renders the form inline.
    # resume_skill = compose_draft so the form's submit message routes
    # the LLM straight back into the draft call with the filled args.
    resume_args: dict[str, Any] = {"template_id": template_id}
    if contact_id:
        resume_args["contact_id"] = int(contact_id)

    _append({
        "type":         "needs_input",
        "source_skill": "compose_check_template_args",
        "title":        f"Noch ein paar Angaben für „{template.get('name', template_id)}\"",
        "context":      "Diese Felder helfen, den Brief vollständig zu machen — Pflichtfelder sind markiert, der Rest ist optional:",
        "fields":       fields_for_form,
        "suggestions":  [],
        # No save-to-contact here — these are template-specific facts,
        # not contact attributes. They don't belong on the contact row.
        "next_playbook_step": "compose_draft",
        "resume_skill": "compose_draft",
        "resume_args":  resume_args,
    })

    req_summary = ", ".join(f["key"] for f in required_fields)
    # Intent prelude — only fires when the template marks one of its
    # fields as body/freeform. The LLM gets one explicit chance to
    # short-circuit the form by pulling from the original user message;
    # if it doesn't, the form is still there as the fallback so the user
    # is never blocked.
    intent_prelude = ""
    if intent_missing:
        keys = ", ".join(im["key"] for im in intent_missing)
        # Collect any template-author-supplied llm_hints for the missing
        # intent fields. Rendered as a bulleted list so per-template
        # writing rules ("casual tone, address by first name, no AI
        # padding") are surfaced verbatim to the LLM without the skill
        # carrying generic rules that fight per-template nuance.
        per_field_rules: list[str] = []
        for im in intent_missing:
            h = (im.get("llm_hint") or "").strip()
            if h:
                per_field_rules.append(f"  • {im['key']}: {h}")
        rules_block = (
            "\nTemplate writing rules for these field(s):\n"
            + "\n".join(per_field_rules) + "\n"
        ) if per_field_rules else ""
        intent_prelude = (
            f"IMPORTANT — before the form is shown, check the user's "
            f"ORIGINAL message in this conversation. The template's "
            f"{keys} field(s) are intent-derived (template marked "
            f"role=body/freeform_text or from_intent=true), meaning the "
            f"user is expected to have ALREADY stated their content in "
            f"their request (e.g. 'tell him we are on holiday in "
            f"Paris'). If they did:\n"
            f"  → Re-call compose_check_template_args with args="
            f"{{{keys.split(',')[0].strip()}: <that intent, lightly "
            f"polished per the rules below>, ...other-already-known-args}}. "
            f"The form will skip that field.\n"
            f"If the user gave no body intent (e.g. they only said "
            f"'write Anna a letter' with no content), let the form ask. "
            f"Do NOT invent intent the user didn't express."
            f"{rules_block}\n"
        )
    hint = (
        f"{intent_prelude}"
        f"{len(required_fields)} REQUIRED template arg(s) missing: {req_summary}.\n"
        "The user sees a form (with both required and optional fields). "
        "Reply ONE short sentence in their language saying you've shown the "
        "form (e.g. 'Brauche noch ein paar Angaben — siehe Formular unten'). "
        "Do NOT ask in prose. Do NOT call compose_draft yet — wait for the "
        "user to submit the form. After they submit (you'll see a "
        "[form_submit] message), proceed DIRECTLY to compose_draft with the "
        "filled args; do NOT re-call compose_check_template_args (the user "
        "is done with the form)."
    )

    return {
        "_llm_hint":   hint,
        "complete":    False,
        "template_id": template_id,
        "missing":     [{"key": f["key"], "label": f["label"],
                          "required": f.get("required", False)}
                          for f in fields_for_form],
    }
