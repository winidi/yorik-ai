"""compose_draft skill — prepare a letter/invoice/email as a draft.

The skill persists a row in `compose_drafts` and emits a
`compose_draft_created` UI action. Chat renders a card with
"Bearbeiten →" that deep-links to /r/compose?draft_id=N where Compose
loads the body, recipient, and subject pre-filled.

The LLM uses this for ANY document-creation intent — letters,
invoices, formal emails. Generating the body inline in the chat bubble
is a dead end (not editable, no template, no Rechnungsnummer, no PDF).
"""
from __future__ import annotations
import json
from typing import Any, List, Optional


_VALID_KINDS = {"letter", "invoice", "offer", "email", "memo"}


# Salutation prefixes — any line STARTING with these counts as chrome
# the template handles, so we strip it from body_text. Case-insensitive.
_SALUTATION_STARTS = (
    "sehr geehrte", "sehr geehrter", "sehr geehrtes",
    "liebe ", "lieber ", "liebes ", "liebste", "liebster",
    "hallo ", "hi ", "hey ", "moin ", "servus",
    "dear ", "to whom",
    "cher ", "chère ", "chers ", "chères ",
    "gentile", "egregio", "egregia",
    "estimado", "estimada",
)

# Closing patterns — any line CONTAINING one of these (or starting with
# it) marks the start of the signature block we strip.
_CLOSING_PATTERNS = (
    "mit freundlichen grüßen", "freundliche grüße", "beste grüße",
    "viele grüße", "liebe grüße", "herzliche grüße",
    "mit besten grüßen",
    "sincerely", "best regards", "kind regards", "regards,", "yours sincerely",
    "cordialement", "cordialmente",
    "saluti", "cordiali saluti", "distinti saluti",
    "atentamente", "saludos cordiales",
)

# Standalone date (DD.MM.YYYY / YYYY-MM-DD / D. Monat YYYY) — also chrome.
import re as _re
_DATE_LINE_RE = _re.compile(
    r"^\s*(?:"
    r"\d{1,2}\.\s*\d{1,2}\.\s*\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\.?\s+(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
    r"|January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{4}"
    r")\s*$",
    _re.IGNORECASE,
)


def _strip_letter_chrome(body: str, sender_name: str = "", recipient_name: str = "") -> str:
    """Strip letter chrome so the template's chrome doesn't duplicate.

    Strategy: a chromed letter is shaped like
        [sender block] [recipient block] [date] [salutation]
        [REAL BODY]
        [closing] [signature]

    We use the salutation as the "real body starts here" marker:
    everything ABOVE it is chrome (drop entirely — the template will
    re-add the recipient block, date, etc.). Then everything from the
    line after the salutation up to (not including) the first closing
    phrase is the actual body content. Anything below the closing is
    signature chrome (drop).

    When no salutation is found we fall back to per-line filtering
    (early returns matter — if the LLM sent JUST a body without chrome,
    we mustn't mangle it).
    """
    if not body or not body.strip():
        return ""

    raw_lines = [l.rstrip() for l in body.splitlines()]
    sender_lower = (sender_name or "").strip().lower()

    # Find salutation index, if any.
    salutation_idx = -1
    for i, line in enumerate(raw_lines):
        low = line.strip().lower()
        if not low:
            continue
        if any(low.startswith(s) for s in _SALUTATION_STARTS):
            salutation_idx = i
            break

    # Find closing index, if any (must come after salutation when both exist).
    closing_idx = -1
    start_search = salutation_idx + 1 if salutation_idx >= 0 else 0
    for i in range(start_search, len(raw_lines)):
        low = raw_lines[i].strip().lower()
        if any(p in low for p in _CLOSING_PATTERNS):
            closing_idx = i
            break

    if salutation_idx >= 0:
        # Body lives strictly between salutation and closing.
        end = closing_idx if closing_idx >= 0 else len(raw_lines)
        middle = raw_lines[salutation_idx + 1 : end]
    elif closing_idx >= 0:
        # Closing without salutation — body is everything above the closing.
        middle = raw_lines[:closing_idx]
    else:
        # No chrome markers — body is the whole thing, fall back to
        # per-line filtering for stray date / sender-name lines.
        middle = raw_lines

    # Per-line cleanup of strays (sender name signature, standalone dates).
    cleaned: list[str] = []
    for line in middle:
        stripped = line.strip()
        if not stripped:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        low = stripped.lower()
        if sender_lower and low == sender_lower:
            continue
        if _DATE_LINE_RE.match(stripped):
            continue
        cleaned.append(stripped)

    # Trim leading + trailing blanks.
    while cleaned and cleaned[0] == "": cleaned.pop(0)
    while cleaned and cleaned[-1] == "": cleaned.pop()
    return "\n".join(cleaned)


# Matches `args.X` inside both expression tags `{{ ... }}` and block
# tags `{% if args.body_text %}` / `{% for p in args.body_text.split ... %}`.
# The block-tag form is critical for body_text: generic-letter.json wraps
# the body in `{% if args.body_text %}`, so a regex limited to `{{ ... }}`
# silently dropped body_text from used_keys and the body fanout no-op'd.
_ARG_REF_RE = _re.compile(r"\{[{%]\s*[^{}%]*?args\.([a-zA-Z_][a-zA-Z0-9_]*)")


async def _embed_yorik_photo_url(url: str, *, user_id: str) -> Optional[str]:
    """Fetch a Yorik /api/photos/{id}/raw URL server-side and return
    a base64 data URL. Used by compose_draft to embed picker-selected
    photos into the rendered HTML so Gotenberg (which can't reach the
    Yorik origin from its container) renders them in the PDF.

    Accepts both Yorik proxy paths (`/api/photos/<id>/raw`) AND raw
    Immich URLs from find_photo's thumbnail_url. Returns None when
    the URL isn't recognizable — caller falls back to the original
    URL (which TipTap can still display in the editor with cookie auth)."""
    import base64 as _b64
    import re as _re2

    # Match Yorik proxy: /api/photos/{asset_id}/raw
    m = _re2.match(r"^/api/photos/([A-Za-z0-9._-]+)/raw\b", url or "")
    if not m:
        return None
    asset_id = m.group(1)

    # Resolve Immich creds the same way the proxy route does: per-user
    # key first (Phase B ACL provisions one per Yorik user), then the
    # global admin key, then the legacy app_settings keys.
    from backend import credential_store as _cs
    from backend import external_users as _xu
    from backend.database import conn_ctx as _cctx, DEFAULT_DB_PATH as _ddb
    creds: Optional[dict[str, Any]] = None
    try:
        creds = _xu.get_user_immich_creds(user_id)
    except Exception:
        creds = None
    if not creds or not creds.get("api_key"):
        creds = _cs.get("immich") or {}
    base_url = (creds.get("base_url") or "").rstrip("/")
    api_key = creds.get("api_key") or ""
    if not (base_url and api_key):
        try:
            with _cctx(_ddb) as conn:
                for k, dest in (("immich_base_url", "base_url"),
                                 ("immich_api_key", "api_key")):
                    row = conn.execute(
                        "SELECT value FROM app_settings WHERE key = ?", (k,),
                    ).fetchone()
                    if row and row["value"]:
                        if dest == "base_url" and not base_url:
                            base_url = row["value"].rstrip("/")
                        elif dest == "api_key" and not api_key:
                            api_key = row["value"]
        except Exception:
            pass
    if not (base_url and api_key):
        return None

    import httpx as _httpx
    upstream = f"{base_url}/api/assets/{asset_id}/original"
    async with _httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(upstream, headers={"x-api-key": api_key})
    if r.status_code != 200:
        return None
    mime = r.headers.get("content-type", "image/jpeg")
    return f"data:{mime};base64,{_b64.b64encode(r.content).decode('ascii')}"


def _template_arg_keys(template: dict[str, Any]) -> set[str]:
    """Return the set of args.X keys actually referenced by the
    template's body_html. Used to skip polyglot fan-out for keys the
    template will never render — keeps the args panel readable."""
    body = template.get("body_html") or ""
    return set(_ARG_REF_RE.findall(body))


def _paragraph_break_body(body: str) -> str:
    """Insert a paragraph break after the first sentence when the body is
    a single multi-sentence paragraph. Catches LLM bodies that merge
    distinct thoughts (greeting + date, request + thanks) into one block.

    Conservative — fires only when:
      • no existing paragraph break (`\\n\\n` absent),
      • body length ≥ 30 chars (avoids "Hi. Bye." / "Yes. No." splits),
      • a sentence boundary (`.`/`!`/`?` + space + capital) exists,
      • both halves around the split are ≥ 5 chars (defends against
        the regex matching on an initial like "Dr. Müller" — second
        half stays too short to be a real sentence).
    """
    if not body or "\n\n" in body or len(body) < 30:
        return body
    # End-of-sentence punctuation + whitespace + next-sentence capital.
    # `[A-ZÀ-ÿ]` covers Latin extended uppercase (German Ä/Ö/Ü included).
    parts = _re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-ÿ])", body, maxsplit=1)
    if len(parts) != 2:
        return body
    if len(parts[0].strip()) < 5 or len(parts[1].strip()) < 5:
        return body
    return parts[0] + "\n\n" + parts[1]


def _set_used(args: dict, key: str, value: Any, used: set[str]) -> None:
    """Set args[key] = value, but only if (a) the template uses this
    key AND (b) it isn't already set. Polyglot fan-out keys for other
    languages don't pollute the args panel."""
    if key not in used:
        return
    if args.get(key) in (None, ""):
        args[key] = value


def _role_arg_keys(
    full_template: Optional[dict[str, Any]],
    role: str,
    contact_group: Optional[str] = None,
) -> List[str]:
    """Return ask_user_for_args arg keys whose `role` matches the given
    role (e.g. "recipient_name"). When `contact_group` is given, narrow
    further to args declaring that group. Used by the role-aware fan-out
    so a contact picked for the "Vermieter" group doesn't also write into
    the "Verwalter" name slot.

    Returns [] when the template has no ask_user_for_args declarations
    (caller falls back to the legacy polyglot alias list)."""
    if not full_template:
        return []
    spec = full_template.get("ask_user_for_args") or []
    if not isinstance(spec, list):
        return []
    out: List[str] = []
    for entry in spec:
        if not isinstance(entry, dict):
            continue
        if entry.get("role") != role:
            continue
        if contact_group is not None:
            if (entry.get("contact_group") or "") != contact_group:
                continue
        key = entry.get("key")
        if isinstance(key, str) and key:
            out.append(key)
    return out


async def execute(
    ctx,
    body: Optional[str] = None,
    kind: str = "letter",
    recipient: Optional[str] = None,
    recipient_address: Optional[str] = None,
    subject: Optional[str] = None,
    template_id: Optional[str] = None,
    contact_id: Optional[int] = None,
    contact_group: Optional[str] = None,
    args: Optional[dict[str, Any]] = None,
    existing_draft_id: Optional[int] = None,
    inline_form: bool = False,
    # Tone-override kwargs. The compose_check_recipient tone hint
    # tells the LLM to pass these for casual letters, but qwen3
    # naturally puts them at the top level (mirroring how `subject`
    # works) rather than inside the args dict. Accept either form and
    # fold them into args before the regular fan-out runs.
    anrede: Optional[str] = None,
    gruss: Optional[str] = None,
) -> dict[str, Any]:
    # Fold top-level tone kwargs into args so downstream code paths only
    # have to look in one place. Caller's args dict wins if it set them
    # explicitly — the kwargs path is the LLM's shortcut, not an override.
    if anrede or gruss:
        args = dict(args or {})
        if anrede and not (args.get("anrede") or "").strip():
            args["anrede"] = anrede
        if gruss and not (args.get("gruss") or "").strip():
            args["gruss"] = gruss
    """Persist a Compose draft and emit `compose_draft_created`.

    Requires an explicit `template_id` chosen via the picker flow
    (list_compose_templates → pick_compose_template → user click), or
    `existing_draft_id` when iterating on a draft (the row carries the
    template_id). Missing both → returns ok=False with a hint pointing
    the caller at the picker flow.
    """
    kind = (kind or "letter").lower().strip()
    if kind not in _VALID_KINDS:
        kind = "letter"

    body_text = (body or "").strip()

    # ── Placeholder gate: detect fake-data the LLM sometimes copies
    # verbatim from training data or from a template's example values.
    # We only catch HIGH-CONFIDENCE placeholder phrases — full names like
    # "Max Mustermann" or bracketed markers like "[adresse]". Substring
    # matches on common road suffixes (e.g. "Musterstraße") would
    # false-positive on real German addresses; we explicitly do NOT
    # include those here.
    #
    # Critical: when this fires, the LLM almost always re-fed the
    # PREVIOUS rendered draft back as body_text. The hint must steer
    # it away from that, not toward asking the user again — the user
    # likely already typed real data and the LLM lost it.
    _PLACEHOLDER_PATTERNS = (
        "vorname nachname",
        "max mustermann", "maria mustermann", "erika mustermann",
        "john doe", "jane doe",
        "[name]", "[address]", "[adresse]", "[straße]", "[strasse]",
        # Sign-off placeholders the LLM leaks at the end of email/letter
        # bodies: "Viele Grüße, [Dein Name]" / "Best, [Your Name]" etc.
        # The template adds the real sender signature; the LLM shouldn't
        # be writing this at all. (Bracketed amount/date stand-ins like
        # [Monat] / [Betrag] are NOT rejected — they're documented as
        # legitimate fill-in markers when the user didn't give a value.)
        "[dein name]", "[ihr name]", "[your name]", "[mein name]",
        "[unterschrift]", "[signature]",
        "<name>", "<address>", "<adresse>", "<straße>", "<strasse>",
        "lorem ipsum",
        "hausverwaltung beispiel",  # specific to our old template defaults
        "beispiel service gmbh",    # specific to our old template defaults
    )
    body_lower = body_text.lower()
    for pat in _PLACEHOLDER_PATTERNS:
        if pat in body_lower:
            return {
                "_llm_hint": (
                    f"REJECTED: your body contains placeholder text ({pat!r}). "
                    "This usually means you copied the template's example values "
                    "into the body. DO NOT do that — template defaults are filled "
                    "at render time from real data (contact addresses, the user's "
                    "profile). RETRY compose_draft now with: body='' (empty — let "
                    "the template's own structure render) OR body containing ONLY "
                    "your custom intro sentences, never names/addresses/dates. "
                    "Pass recipient via contact_id (from find_contact). Pass any "
                    "missing facts via the args dict, not by writing them into body."
                ),
                "rejected": True,
                "reason":   "placeholder_content",
                "matched_phrase": pat,
            }

    # ── Require explicit template_id: the only way to choose a template
    # is the picker flow (list_compose_templates → pick_compose_template
    # → user click). Iterating on an existing draft is the one exception
    # — the draft row supplies the template_id below.
    if not template_id and not existing_draft_id:
        return {
            "ok": False,
            "_llm_hint": (
                "MISSING_TEMPLATE_ID: compose_draft requires an explicit "
                "template_id. Call list_compose_templates to see what's "
                "available, read each template's description and "
                "when_to_use, then call pick_compose_template with the 3 "
                "best fits. The user clicks one; you'll see a "
                "`[template_picked id=X]` reply; then call compose_draft "
                "with that id."
            ),
        }

    # ── Picker-wait guard: if pick_compose_template emitted a
    # template_picker this turn, the user hasn't clicked yet — the
    # contract is to wait for the [template_picked id=X] follow-up.
    # Otherwise the chat shows both a picker AND a needs_input form
    # simultaneously and the user can't tell which one is live.
    # existing_draft_id skips the check (iterating on a locked draft).
    if not existing_draft_id:
        try:
            from backend.ui_tools import get_ui_actions as _get_pending_ui
            for _act in (_get_pending_ui() or []):
                if isinstance(_act, dict) and _act.get("type") == "template_picker":
                    return {
                        "ok": False,
                        "_llm_hint": (
                            "PICKER_PENDING: pick_compose_template just "
                            "rendered a template_picker card this turn. "
                            "STOP — wait for the user to click and send a "
                            "`[template_picked id=X]` follow-up before "
                            "calling compose_draft. Reply ONE short "
                            "sentence in the user's language asking "
                            "them to pick (e.g. 'Welche Vorlage passt?' / "
                            "'Which template?'); do NOT enumerate the "
                            "candidates in prose; do NOT call "
                            "compose_draft again this turn."
                        ),
                    }
        except Exception:
            pass

    # Convert plain-text body into <p> paragraphs so TipTap renders it cleanly.
    # If the body already has HTML tags, leave it alone.
    if "<" not in body_text:
        paragraphs = [p.strip() for p in body_text.split("\n\n") if p.strip()]
        body_html = "".join(f"<p>{p}</p>" for p in paragraphs)
    else:
        body_html = body_text

    user_id = getattr(ctx, "user_id", 1)

    from backend.database import conn_ctx, DEFAULT_DB_PATH

    # If the LLM passed a contact_id (after calling find_contact first —
    # see the system prompt), pull the contact's display_name + address
    # from the identity hub. We write the address into args_json under
    # several common template keys (recipient_address, empfaenger_adresse,
    # …) so any template's recipient block gets filled automatically.
    # Read the sender's country up-front so we can decide whether to
    # include the recipient's country in the rendered address.
    # Domestic conventions: DE→DE doesn't print "DE" on either party.
    # We default to "DE" so the common case Just Works without a config.
    sender_country = "DE"
    try:
        from backend.database import conn_ctx as _cctx, DEFAULT_DB_PATH as _ddb
        with _cctx(_ddb) as _c:
            _row = _c.execute(
                "SELECT country FROM user_profiles WHERE id = ?",
                (getattr(ctx, "user_id", 1),),
            ).fetchone()
            if _row and _row["country"]:
                sender_country = _row["country"]
    except Exception:
        pass

    address_str = ""
    recipient_country: str = ""
    contact_obj: Optional[dict] = None
    if contact_id:
        try:
            from backend import contacts as _contacts_mod
            contact_obj = _contacts_mod.get(int(contact_id))
            if contact_obj:
                # Recipient defaults to the contact's name unless the LLM
                # explicitly overrode it.
                if not recipient:
                    recipient = contact_obj["display_name"]
                addrs = contact_obj.get("addresses") or []
                if addrs:
                    a = sorted(
                        addrs,
                        key=lambda x: ["home", "work", "billing", "shipping"].index(x.get("kind", "other"))
                        if x.get("kind") in ("home", "work", "billing", "shipping") else 4,
                    )[0]
                    recipient_country = (a.get("country") or "").strip()
                    # Only show country when it differs from sender's —
                    # domestic letters skip it (German convention).
                    show_country = (
                        recipient_country
                        and recipient_country.upper() != sender_country.upper()
                    )
                    parts = [
                        a.get("line1"),
                        a.get("line2"),
                        " ".join(filter(None, [a.get("postcode"), a.get("city")])),
                        a.get("region"),
                        a.get("country") if show_country else None,
                    ]
                    address_str = "\n".join(p for p in (
                        (s or "").strip() for s in parts
                    ) if p)
        except Exception:
            # Lookup failures must not break draft creation — the chat
            # card just won't have the pre-filled address.
            contact_obj = None

    # Load template up-front so we know which arg keys it actually uses.
    # Polyglot fan-out (vermieter_*, locatore_*, sprzedawca_*, …) gets
    # filtered against this so the args panel only shows relevant fields.
    full_template: Optional[dict[str, Any]] = None
    if template_id:
        try:
            from backend.compose import templates as _tpl_mod
            full_template = _tpl_mod.get(template_id)
            # Label-to-id fallback: the LLM regularly confuses template
            # `name` ("Brief (allgemein)") with `id` ("generic-letter")
            # after reading the alternates list.
            if full_template is None:
                _target = template_id.strip().lower()
                for _t in _tpl_mod.load_all():
                    _d = _t if isinstance(_t, dict) else (
                        _tpl_mod.public_dict(_t) if hasattr(_tpl_mod, "public_dict") else None
                    )
                    if not _d:
                        continue
                    if (_d.get("name") or "").strip().lower() == _target:
                        full_template = _d
                        template_id = _d.get("id") or template_id
                        break
        except Exception:
            full_template = None
    # Hallucinated-id error path: the LLM passed a non-empty template_id
    # that doesn't match any template. Refuse loudly instead of silently
    # falling through to generic-letter — that silent swap caused the
    # chat-audit's compose_check_recipient loop where the skill ran
    # against generic-letter while the LLM thought it was on a different
    # template, and the readiness check kept disagreeing.
    if full_template is None and (template_id or "").strip():
        return {
            "ok":        False,
            "error":     f"template not found: {template_id!r}",
            "_llm_hint": (
                f"UNKNOWN_TEMPLATE_ID: {template_id!r} is not a known "
                "template id. Call list_compose_templates to see real "
                "ids, then pick_compose_template with the 3 best fits — "
                "the user picks via the chat card."
            ),
        }
    if full_template is None:
        # No template_id and the LLM didn't infer one — fall back to
        # the bundled generic letter so we always have something to
        # render. Prefer the English default (the only generic letter
        # in the bundled-default install since 2026-06); fall through
        # to the German id when that's what the operator installed
        # from marketplace; finally accept None and let the caller
        # surface a clear "no templates installed" error.
        try:
            from backend.compose import templates as _tpl_mod
            for _fallback_id in ("generic-letter-en", "generic-letter"):
                try:
                    full_template = _tpl_mod.get(_fallback_id)
                except _tpl_mod.TemplateError:
                    continue
                if full_template is not None:
                    template_id = _fallback_id
                    break
        except Exception:
            pass

    # Override kind from the template when it declares one. The LLM
    # regularly sets kind="letter" because the user said "Brief" but
    # then picks template_id="generic-email" — chat then rendered the
    # card labeled "Brief" while the template was actually email. The
    # template knows what it is; we read it directly. Falls back to the
    # LLM-supplied kind for older templates that don't declare one.
    if full_template:
        template_kind = (full_template.get("kind") or "").strip().lower()
        if template_kind in _VALID_KINDS:
            kind = template_kind

    used_keys = _template_arg_keys(full_template) if full_template else set()

    # Seed args dict with the LLM-supplied generic args (the new `args`
    # parameter). The LLM uses this to set structured slots like
    # beendigung_zum / mietvertrag_vom / kuendigung_zum directly,
    # instead of stuffing dates into prose body_text where the template
    # can't pick them up.
    args_in = dict(args or {})

    # If this is an UPDATE to an existing draft, load its args FIRST
    # so we preserve prior fields the user/LLM filled earlier. The LLM
    # only needs to send the keys it's CHANGING — everything else stays.
    # Same for recipient / subject / template_id / kind so a one-field
    # tweak ("ändere Datum auf 31.12") doesn't reset the whole letter.
    existing_args: dict[str, Any] = {}

    # ── Recent-draft auto-resume ──
    # Small models (Qwen 9b in particular) regularly forget the
    # just-created draft_id between turns. Two failure modes:
    #   (1) LLM passes a stale/hallucinated existing_draft_id ("=1" when
    #       the current draft is 14) → wrong draft gets edited.
    #   (2) LLM passes no existing_draft_id at all → a new draft is
    #       created for what was clearly a refine ("mach freundlicher").
    # When the user has a draft updated in the last 90 seconds AND the
    # current call would target a different one (or none), prefer the
    # recent draft — BUT only if the new call looks like the same
    # compose intent. Otherwise (different template, different recipient,
    # different contact) treat it as a brand-new compose and INSERT.
    # Without the intent gate, back-to-back tests with unrelated
    # intents would hijack each other's drafts.
    try:
        _uid = getattr(ctx, "user_id", 1)
        from backend.database import conn_ctx as _c1, DEFAULT_DB_PATH as _db1
        with _c1(_db1) as _c:
            _recent = _c.execute(
                "SELECT id, updated_at, template_id, recipient "
                "FROM compose_drafts "
                "WHERE user_id = ? "
                "  AND updated_at > datetime('now', '-90 seconds') "
                "ORDER BY updated_at DESC LIMIT 1",
                (_uid,),
            ).fetchone()
        if _recent and int(_recent["id"]) != int(existing_draft_id or 0):
            # Intent match — vote on each field. A match (+1), a mismatch
            # (-1), or abstain (0 — one side is empty). Resume only when
            # there are no mismatches AND at least one positive match.
            # OR-style logic was too permissive: two fresh generic-letter
            # intents with different recipients both voted "template
            # matches" and hijacked each other (Test Q + R).
            new_recipient_str = str(recipient or "").strip()
            new_template_id = (template_id or "").strip()
            old_recipient_str = str(_recent["recipient"] or "").strip()
            old_template_id = (_recent["template_id"] or "").strip()

            def _vote(new_v: str, old_v: str) -> int:
                if not new_v or not old_v:
                    return 0
                return 1 if new_v == old_v else -1

            t_vote = _vote(new_template_id, old_template_id)
            r_vote = _vote(new_recipient_str, old_recipient_str)
            intent_matches = (
                t_vote != -1
                and r_vote != -1
                and (t_vote == 1 or r_vote == 1)
            )
            import logging as _log
            if intent_matches:
                _log.getLogger("yorik.compose_draft").info(
                    "compose_draft auto-resume: LLM passed existing_draft_id=%s "
                    "but user's most-recent draft (%s, updated %s) is fresher AND "
                    "intent matches (template=%r recipient=%r) — treating as refine",
                    existing_draft_id, _recent["id"], _recent["updated_at"],
                    new_template_id, new_recipient_str,
                )
                existing_draft_id = int(_recent["id"])
            else:
                _log.getLogger("yorik.compose_draft").info(
                    "compose_draft auto-resume DECLINED: most-recent draft (%s) "
                    "has template=%r recipient=%r but this call has template=%r "
                    "recipient=%r — different intent, will INSERT new draft",
                    _recent["id"], old_template_id, old_recipient_str,
                    new_template_id, new_recipient_str,
                )
    except Exception:
        pass  # fall through to original handling

    if existing_draft_id:
        try:
            from backend.database import conn_ctx as _cctx2, DEFAULT_DB_PATH as _ddb2
            with _cctx2(_ddb2) as _c:
                _row = _c.execute(
                    "SELECT user_id, kind, template_id, recipient, subject, args_json "
                    "FROM compose_drafts WHERE id = ?",
                    (int(existing_draft_id),),
                ).fetchone()
            if _row:
                # Ownership check — same shape as the GET endpoint.
                if _row["user_id"] != getattr(ctx, "user_id", 1):
                    existing_draft_id = None  # silently treat as create-new
                else:
                    import json as _json2
                    existing_args = _json2.loads(_row["args_json"] or "{}") or {}
                    if not recipient and _row["recipient"]:
                        recipient = _row["recipient"]
                    if not subject and _row["subject"]:
                        subject = _row["subject"]
                    if not template_id and _row["template_id"]:
                        template_id = _row["template_id"]
                    if not kind and _row["kind"]:
                        kind = _row["kind"]
        except Exception:
            existing_args = {}
            existing_draft_id = None

    args = {"recipient": recipient or "", "subject": subject or ""}
    # Apply existing args FIRST (the prior draft state), then LLM-supplied
    # args on top (so the LLM can override specific fields it wants to change).
    for k, v in existing_args.items():
        if v not in (None, ""):
            args[k] = v
    for k, v in args_in.items():
        if v not in (None, ""):
            args[k] = v

    # ── Tone auto-detect — informal anrede + gruss when the body is
    # casual and the recipient is a person on first-name basis. The
    # compose_check_recipient tone hint tries to coax the LLM into
    # passing these in args, but qwen3 frequently forgets it across
    # intervening tool calls (especially after a needs_input form
    # round-trip). Server-side detect-and-apply is the reliable path.
    # Heuristic: original body contains German du-form pronouns OR an
    # informal greeting AND the contact is kind=person with a known
    # first name AND the LLM hasn't explicitly set anrede/gruss. We do
    # this BEFORE the chrome-strip + fan-out so anrede/gruss land in
    # args before template_default_args have a chance to fill empty
    # slots with the formal "Sehr geehrte Damen und Herren,".
    if body and contact_obj and (contact_obj.get("kind") == "person"):
        raw_low = body.lower()
        # German du-form: " du " / "dich" / "dir" / "dein…" plus the
        # most common informal greetings the LLM emits.
        _DU_TOKENS = (" du ", " dich", " dir", " dein", " deine",
                      " deiner", " deinen", " deines",
                      "hallo ", "liebe ", "lieber ", "liebste",
                      "wie geht", "hey ")
        if any(t in raw_low for t in _DU_TOKENS):
            tone_first = (contact_obj.get("first_name")
                          or (contact_obj.get("display_name") or "").split(" ")[0]
                          or "").strip()
            if tone_first:
                # _FORMAL_DEFAULTS guards us from overriding when the
                # LLM (or a prior turn's existing_args) deliberately
                # set a different anrede. Empty AND template-default
                # both qualify as "no opinion yet."
                _FORMAL_DEFAULTS = {
                    "sehr geehrte damen und herren,",
                    "sehr geehrte damen und herren",
                    "",
                }
                cur_an = (args.get("anrede") or "").strip().lower()
                if cur_an in _FORMAL_DEFAULTS:
                    args["anrede"] = f"Hallo {tone_first},"
                cur_gr = (args.get("gruss") or "").strip().lower()
                _FORMAL_GRUSS = {
                    "mit freundlichen grüßen",
                    "mit freundlichen gruessen",
                    "",
                }
                if cur_gr in _FORMAL_GRUSS:
                    args["gruss"] = "Liebe Grüße"

    # ── Map the LLM's free-form `body` parameter into the template's
    # body-text arg slot. Without this, calling compose_draft with
    # body='hiermit fordere ich…' against the generic-letter template
    # (which has no structured Mahnung fields and renders {{ args.body_text }})
    # produced an EMPTY letter — the placeholder text appeared because
    # args.body_text was never populated. The LLM's intent prose got
    # silently dropped on the floor.
    #
    # Fan out to every reasonable body-text-shaped arg slot, but only
    # ones the active template actually references (used_keys gates it
    # the same way recipient address fanout does). Common slots across
    # the bundled templates: body_text, freitext, text, body.
    if body_text:
        # Strip chrome (salutation + closing + standalone date lines)
        # BEFORE fan-out. Otherwise the raw body lands in args.body_text
        # via _set_used, and the later strip pass can't overwrite (its
        # result is run through _set_used which is empty-only). Result
        # was double greetings: template prints anrede="Sehr geehrte…"
        # AND body_text still starts with "Liebe Anna,\n\n…", same for
        # the closing. sender_name isn't known yet at this point (the
        # profile fan-out runs later) — pass "" and let the strip still
        # catch salutation/closing lines; sender-line filtering is a
        # minor extra the strip does when it has a name to compare.
        body_text = _strip_letter_chrome(body_text, "", recipient or "")
        # Paragraph-break a single-paragraph multi-sentence body. The
        # LLM frequently merges thoughts ("Ich liebe dich. Freue mich
        # auf Samstag.") into one line — emails read awkwardly without
        # a break between distinct topics. Conservative: only fires
        # when there's no existing \n\n and ≥2 sentences. MUST run
        # before the _set_used fan-out below; doing it later is a
        # no-op (_set_used is empty-only and won't overwrite).
        body_text = _paragraph_break_body(body_text)
    if body_text:
        for key in ("body_text", "freitext", "text", "body"):
            _set_used(args, key, body_text, used_keys)

    # Subject fan-out. LLM passes `subject="…"` (English top-level field
    # on the draft row); templates declare the subject slot under
    # different keys depending on language (betreff, oggetto, sujet,
    # temat, asunto). Without this fan-out generic-letter renders with
    # an empty Betreff line even though the chat says the subject was
    # set — args.subject stored on the row, args.betreff stays None,
    # Jinja silently drops it. MUST run before the compose_check_template_args
    # preflight so a draft created with subject="X" passed in doesn't
    # trip the "betreff missing" form.
    subj_value = (subject or args.get("subject") or "").strip()
    if subj_value:
        # Path 1 — role-aware: fill every key declared with role="subject"
        # in ask_user_for_args. These slots are "used" by the template's
        # form + preflight even when body_html itself only references the
        # English `args.subject` via subject_template — without this pass
        # the LLM provides a subject, _set_used skips args.betreff because
        # body_html doesn't reference it, and preflight refuses on
        # "betreff missing" while the user wonders why the form keeps
        # showing up. role="subject" entries are exactly the language-
        # variant slots the template author intended to receive a value.
        if full_template:
            for key in _role_arg_keys(full_template, "subject"):
                if not args.get(key):
                    args[key] = subj_value
                    used_keys.add(key)
        # Path 2 — legacy polyglot list: gated by _set_used (body_html
        # must reference the key). Preserves the old "don't pollute the
        # args panel with unused language variants" rule for community
        # templates that pre-date the ask_user_for_args role declaration.
        for key in ("subject", "betreff", "oggetto", "sujet",
                     "temat", "asunto"):
            _set_used(args, key, subj_value, used_keys)

    # Recipient-address fanout — used to live inside `if body_text:` which
    # silently dropped the contact's address whenever the caller passed an
    # empty body (the form-only flow). Pull it out so the address ALWAYS
    # propagates when a contact is resolved + has a postal address.
    if address_str:
        # Role-aware fill: prefer args declared with role=recipient_address
        # in ask_user_for_args. When contact_group is set, narrow to that
        # group so multi-recipient templates (e.g. Arbeitgeber + HR) don't
        # all collapse to the same address. Fall back to the legacy
        # polyglot alias list ONLY if the template has no role declarations,
        # which preserves behavior for old/community templates.
        role_keys = _role_arg_keys(full_template, "recipient_address", contact_group)
        if role_keys:
            for key in role_keys:
                _set_used(args, key, address_str, used_keys)
        else:
            for key in (
                "recipient_address", "recipient_address_line1",
                "empfaenger_adresse", "vermieter_adresse", "anbieter_adresse",
                "locatore_indirizzo", "conduttore_indirizzo",
                "sprzedawca_adres", "wynajmujacy_adres",
            ):
                _set_used(args, key, address_str, used_keys)
        # `recipient_address` always wins since the editor itself uses
        # it for the recipient picker, regardless of template.
        if args.get("recipient_address") in (None, ""):
            args["recipient_address"] = address_str
    if contact_obj:
        name = contact_obj["display_name"]
        role_keys = _role_arg_keys(full_template, "recipient_name", contact_group)
        if role_keys:
            for key in role_keys:
                _set_used(args, key, name, used_keys)
        else:
            for key in (
                "recipient_name", "empfaenger_name", "vermieter_name",
                "anbieter_name", "locatore_nome", "sprzedawca_nazwa",
                "wynajmujacy_imie_nazwisko",
            ):
                _set_used(args, key, name, used_keys)
        if args.get("recipient_name") in (None, ""):
            args["recipient_name"] = name

    # ── Auto-fill the SENDER block from the calling user's profile.
    # Templates name the sender field differently (sender_name /
    # absender_name / mieter_name / konduttore_nome / …). Fan out the
    # user's profile data into every common key the same way we do for
    # recipients. Saves the LLM from having to ask "wie heißt du?" when
    # we already know from onboarding.
    try:
        with conn_ctx(DEFAULT_DB_PATH) as conn:
            urow = conn.execute(
                "SELECT first_name, last_name, name, address_street, "
                "       address_postcode, address_city, country, "
                "       business_name, phone, signature_data_url, "
                "       tax_id, iban "
                "FROM user_profiles WHERE id = ?",
                (user_id,),
            ).fetchone()
        if urow:
            sender_name = " ".join(filter(None, [
                urow["first_name"], urow["last_name"],
            ])).strip() or urow["name"] or ""
            # Sender block — omit own country when it matches sender_country
            # (the "from a German address writing to a German recipient,
            # don't bother printing DE" convention). When the recipient
            # IS abroad, we show the sender's country too so the foreign
            # postal service can route the reply.
            show_sender_country = bool(
                recipient_country
                and recipient_country.upper() != sender_country.upper()
            )
            sender_addr_parts = [
                urow["address_street"],
                " ".join(filter(None, [urow["address_postcode"], urow["address_city"]])),
                urow["country"] if show_sender_country else None,
            ]
            sender_addr = "\n".join(p for p in (
                (s or "").strip() for s in sender_addr_parts
            ) if p)
            if sender_name:
                # Same pattern as recipient_*: prefer role-declared args
                # so templates with explicit role=sender_name win cleanly.
                # Legacy fallback for templates without ask_user_for_args.
                role_keys = _role_arg_keys(full_template, "sender_name")
                if role_keys:
                    for key in role_keys:
                        _set_used(args, key, sender_name, used_keys)
                else:
                    for key in (
                        "sender_name", "absender_name", "mieter_name",
                        "konduttore_nome", "konsument_imie_nazwisko",
                        "wierzyciel_nazwa",
                    ):
                        _set_used(args, key, sender_name, used_keys)
                # First/last name splits — same gating.
                if urow["first_name"]:
                    _set_used(args, "sender_first_name", urow["first_name"], used_keys)
                if urow["last_name"]:
                    _set_used(args, "sender_last_name", urow["last_name"], used_keys)
            if sender_addr:
                role_keys = _role_arg_keys(full_template, "sender_address")
                if role_keys:
                    for key in role_keys:
                        _set_used(args, key, sender_addr, used_keys)
                else:
                    for key in (
                        "sender_address", "absender_adresse", "mieter_adresse",
                        "konduttore_indirizzo", "konsument_adres",
                        "wierzyciel_adres",
                    ):
                        _set_used(args, key, sender_addr, used_keys)
            if urow["business_name"]:
                _set_used(args, "sender_business_name", urow["business_name"], used_keys)
            if urow["phone"]:
                _set_used(args, "sender_phone", urow["phone"], used_keys)
            # Sender city → `ort` (the German "place" line above the date).
            # Templates almost always default ort to user's city; the LLM
            # shouldn't have to ask "in welchem Ort warst du als du das
            # geschrieben hast" when we know exactly where they live.
            if urow["address_city"]:
                _set_used(args, "ort", urow["address_city"], used_keys)
            # Scanned signature — only fan when the template renders it.
            if urow["signature_data_url"]:
                for key in ("signature_image_url", "unterschrift_url"):
                    _set_used(args, key, urow["signature_data_url"], used_keys)
            # Tax id + IBAN: previously absent from the auto-fill set, so
            # rechnung-de / invoice-en forms kept asking for them even
            # when the profile had them. §14 UStG flags the Steuernummer/
            # USt-IdNr as Pflichtangabe, so showing an empty form field
            # for it when we already KNOW the value is a UX miss. Fan
            # tax_id to every common Steuernr/USt-IdNr key so whichever
            # the template references gets filled (templates use just
            # one or the other; this doesn't double-render — _set_used
            # only writes referenced keys).
            if urow["tax_id"]:
                for key in (
                    "absender_steuernr", "absender_ustid",
                    "sender_tax_id", "sender_tax_number",
                ):
                    _set_used(args, key, urow["tax_id"], used_keys)
            if urow["iban"]:
                for key in ("absender_iban", "sender_iban"):
                    _set_used(args, key, urow["iban"], used_keys)
    except Exception:
        # Sender auto-fill is a nice-to-have — never break draft
        # creation if the profile read trips.
        pass

    # ── CRITICAL: when a template is picked, ACTUALLY render through it.
    # Earlier versions stored the LLM's body_html as-is, ignoring the
    # template entirely — so picking "Brief (allgemein)" produced just
    # a body without the letterhead, recipient block, salutation or
    # closing. Now we feed the LLM's text into `args.body_text` and run
    # the template's Jinja, so the final saved HTML has the full
    # template chrome wrapped around the LLM's content.
    # ── Missing required template args ────────────────────────────────
    # Two paths depending on whether the caller is the Compose editor or
    # the main chat:
    #
    #   - **Compose mode** (`inline_form=True` set by the LLM when it
    #     sees `[Compose context: ...]`, OR `existing_draft_id` set —
    #     user is editing a saved draft): emit `needs_input` form and
    #     refuse to draft. The Compose UI renders the form inline as
    #     part of the editor flow — this is the right UX there.
    #
    #   - **Chat mode** (default): create the draft anyway with empty
    #     slots (Jinja's ChainableUndefined renders missing vars as ""),
    #     surface the missing keys via `compose_draft_created.missing_args`
    #     so the chat card shows "Noch leer: X — in Compose öffnen".
    #     A chat-blocking form was too disruptive for casual letter
    #     requests; the user can finish in the Compose editor.
    #
    # needs_input is still reserved for missing recipient address —
    # see compose_check_recipient. That's the one thing the template
    # genuinely can't fake regardless of caller.
    missing_required_args: List[str] = []
    if full_template and full_template.get("ask_user_for_args"):
        ask_spec = full_template["ask_user_for_args"]
        if isinstance(ask_spec, list):
            for entry in ask_spec:
                if not isinstance(entry, dict): continue
                if not entry.get("required"): continue
                key = entry.get("key")
                if not key: continue
                v = args.get(key)
                if v is None or (isinstance(v, str) and not v.strip()):
                    missing_required_args.append(key)

    use_form = bool(inline_form) or bool(existing_draft_id)
    if missing_required_args and use_form and full_template:
        # Compose-mode path: emit the inline form (ComposeAgentChat
        # renders it via NeedsInputCard) and REJECT the draft so the
        # LLM waits for [form_submit from=compose_draft].
        form_fields = []
        for entry in full_template.get("ask_user_for_args") or []:
            if not isinstance(entry, dict): continue
            k = entry.get("key")
            if not k: continue
            current = args.get(k) or ""
            field = {
                "key":      k,
                "label":    entry.get("label") or k,
                "required": bool(entry.get("required", False)),
                "value":    current if isinstance(current, str) else "",
            }
            if entry.get("pattern"): field["pattern"] = entry["pattern"]
            if entry.get("hint"):    field["hint"]    = entry["hint"]
            if entry.get("input"):   field["input"]   = entry["input"]
            form_fields.append(field)
        from backend.ui_tools import _append
        _append({
            "type":         "needs_input",
            "source_skill": "compose_draft",
            "title":        f"Noch Angaben fehlen für „{full_template.get('name', template_id)}\"",
            "context":      "Diese Pflichtfelder brauche ich, damit der Brief korrekt erstellt wird. Optionale Felder kannst du leer lassen.",
            "fields":       form_fields,
            "suggestions":  [],
            "next_playbook_step": "compose_draft",
            "resume_skill": "compose_draft",
            "resume_args":  {
                "contact_id":        contact_id,
                "template_id":       template_id,
                "existing_draft_id": existing_draft_id,
                "inline_form":       True,
            },
        })
        return {
            "_llm_hint": (
                "REJECTED: compose_draft refuses to render in Compose mode "
                f"because these REQUIRED template args are missing: "
                f"{', '.join(missing_required_args)}. A form has been shown "
                "to the user automatically — DO NOT call compose_draft "
                "again until you see a [form_submit from=compose_draft] "
                "message. Reply ONE short sentence in the user's language "
                "(e.g. 'Mir fehlen noch ein paar Angaben — siehe Formular unten')."
            ),
            "rejected":   True,
            "reason":     "missing_required_template_args",
            "missing":    missing_required_args,
            "template_id": template_id,
        }
    # Chat-mode path falls through — the draft will render with empty
    # slots and missing_required_args is surfaced in compose_draft_created.

    # ── Inline image embedding ───────────────────────────────────────
    # If args.inline_image_url is a Yorik proxy URL (or any non-data
    # URL), fetch it server-side and replace with a base64 data URL.
    # Why: Gotenberg renders the PDF in a separate container and can't
    # reach `localhost:8000`. Embedding the bytes inline makes the
    # rendered HTML (and therefore the PDF) self-contained.
    _img_url = args.get("inline_image_url") if isinstance(args.get("inline_image_url"), str) else ""
    if _img_url and not _img_url.startswith("data:"):
        try:
            _embedded = await _embed_yorik_photo_url(_img_url, user_id=user_id)
            if _embedded:
                args["inline_image_url"] = _embedded
        except Exception as _exc:
            import logging as _lg
            _lg.getLogger("yorik.compose_draft").warning(
                "inline image embed failed for %r: %s", _img_url, _exc,
            )

    # ── Subject auto-generation ──────────────────────────────────────
    # If the LLM didn't pass a subject AND the template declares a
    # `subject_template` Jinja string, render it. Saves the LLM from
    # having to guess a subject line for every Kündigung / Rechnung.
    #
    # Gated: skip the render when the template's `ask_user_for_args`
    # declares a field with `role="subject"`. Those templates expect the
    # subject to come from the user/LLM via the intent_prelude in
    # compose_check_template_args. If we auto-fill with subject_template's
    # placeholder branch (e.g. "Email" / "E-Mail"), the preflight sees a
    # non-empty subject and never triggers the intent push — that's how
    # "subject = 'Email'" ended up in stored drafts.
    _declares_user_subject = any(
        isinstance(e, dict) and e.get("role") == "subject"
        for e in (full_template.get("ask_user_for_args") or [])
    ) if full_template else False
    if full_template and not (args.get("subject") or "").strip() and not _declares_user_subject:
        subj_tpl = full_template.get("subject_template")
        if subj_tpl:
            try:
                from jinja2 import Environment, ChainableUndefined
                env = Environment(undefined=ChainableUndefined)
                rendered_subject = env.from_string(subj_tpl).render(args=args).strip()
                if rendered_subject:
                    args["subject"] = rendered_subject
                    subject = rendered_subject
            except Exception:
                pass

    # Auto-expand a bare salutation prefix into a proper anrede line.
    # Users typing into the form regularly enter just "Herr" or "Frau"
    # (the field label nudges them toward a salutation but they read
    # it as a title prefix). Without expansion the rendered letter
    # shows the bare word in place of "Sehr geehrter Herr Müller,".
    _bare_anr = (args.get("anrede") or "").strip()
    if _bare_anr.lower().rstrip(".,") in ("herr", "frau", "herrn"):
        recip_name = (args.get("empfaenger_name") or recipient or "").strip()
        # Last word of the recipient as the surname is right ~95% of the
        # time for "Vorname Nachname" / "Dr. med. Nachname" / "Firma X".
        surname = recip_name.rsplit(" ", 1)[-1] if recip_name else ""
        prefix = "Sehr geehrter Herr" if _bare_anr.lower().startswith("herr") \
                 else "Sehr geehrte Frau"
        args["anrede"] = (f"{prefix} {surname}," if surname else f"{prefix},")

    # ── Preflight: required template args MUST be filled ──────────────
    # The LLM regularly shortcuts to compose_draft without first running
    # compose_check_template_args (and compose_check_recipient before
    # that), landing drafts with empty Betreff / body_text / address
    # because it skipped the form. Delegate the check to the dedicated
    # skill; if it returns complete=false, the inline form has already
    # been emitted as a needs_input ui_action — relay the hint to the
    # LLM and DON'T create the draft. Only runs for FRESH drafts —
    # iterating on an existing_draft_id has its own resume-via-form path.
    if (not existing_draft_id) and template_id and full_template and ctx is not None:
        try:
            cta_result = await ctx.call_skill(
                "compose_check_template_args",
                template_id=template_id,
                contact_id=contact_id,
                args=dict(args or {}),
            )
            # Merge back the augmented args from the preflight — the
            # check skill does Bug 4 extraction, applies template_defaults,
            # leistungsdatum_von ← rechnungsdatum, etc. Without merging
            # back into `args` here, all that work happens in its local
            # scope and the LLM's stale guess wins downstream (the
            # rendered draft showed €0,00 even though we DID extract
            # the user-stated €420). LLM-provided values win on
            # conflict (left side of **), CTA's augmentation fills gaps.
            if isinstance(cta_result, dict):
                augmented = cta_result.get("args") or {}
                if isinstance(augmented, dict) and augmented:
                    args = {**augmented, **args}
            # positions[] extraction wins over flat position_<N>_*
            # hallucinations. When the 9B is told to invoke compose_draft
            # for a multi-item Rechnung, it routinely splits the text
            # into position_1_beschreibung / _2_ / _3_ flat keys AND
            # the extraction skill also emits positions=[...] — both
            # end up in args after the merge, and the template's
            # has_flat guard then prefers the flat keys (which often
            # have einzelpreis="0" because the LLM crammed price into
            # beschreibung). When extraction produced a real
            # positions[] array, drop the flat-key hallucinations so
            # the new Jinja branch fires.
            _pos_value = args.get("positions")
            if (
                isinstance(_pos_value, list)
                and _pos_value
                and any(isinstance(item, dict) and item for item in _pos_value)
            ):
                _flat_suffixes = (
                    "_beschreibung", "_description",
                    "_menge", "_quantity",
                    "_einheit", "_unit",
                    "_einzelpreis", "_unit_price",
                )
                for _k in list(args.keys()):
                    if (
                        _k.startswith("position_")
                        and any(_k.endswith(suf) for suf in _flat_suffixes)
                    ):
                        del args[_k]
            if isinstance(cta_result, dict) and not cta_result.get("complete", True):
                # Build a short, headline-first refusal. The 9B model
                # routinely misread the previous multi-paragraph refusal
                # (which buried "REFUSED" inside paragraphs about "form
                # shown to user") as success and replied "Brief erstellt"
                # while no draft existed.
                #
                # New shape: starts with NOT CREATED, names the missing
                # args, gives the exact call signature to retry with.
                # No advice prose, no "shown_to_user" phrasing.
                missing_keys = [
                    f.get("key", "?")
                    for f in (cta_result.get("fields") or [])
                    if f.get("required")
                ]
                if not missing_keys:
                    missing_keys = ["(see compose_check_template_args result)"]
                missing_str = ", ".join(missing_keys)
                return {
                    "ok": False,
                    "needs_input_for": template_id,
                    "_llm_hint": (
                        f"DRAFT NOT CREATED — required template args missing: "
                        f"{missing_str}. The inline form has been rendered to "
                        f"the user as fallback; if the user's earlier messages "
                        f"already gave you these values, re-call compose_draft "
                        f"NOW with template_id={template_id!r}, body=\"\", "
                        f"args={{...filled fields...}}. If not, reply ONE "
                        f"short sentence asking them to fill the form "
                        f"(e.g. 'Brauche noch ein paar Angaben — siehe "
                        f"Formular unten') and wait for "
                        f"`[form_submit from=compose_check_template_args]`. "
                        f"Do NOT claim the draft was created — it was not."
                    ),
                }
        except Exception as _exc:  # noqa: BLE001
            # Preflight is best-effort — a failure on the check side must
            # not block the draft. Worst case the LLM lands an incomplete
            # draft (the pre-preflight behaviour).
            import logging as _log_pre
            _log_pre.getLogger("yorik.compose_draft").warning(
                "compose_draft preflight check failed (continuing): %s", _exc,
            )

    rendered_body_html: Optional[str] = None
    if full_template:
        try:
            from backend.compose import render as _rdr

            # Order matters here: fan out OUR data FIRST so the empty
            # template defaults don't block our setdefault calls.
            if recipient:
                for key in ("recipient_name", "empfaenger_name",
                             "vermieter_name", "anbieter_name",
                             "locatore_nome", "sprzedawca_nazwa"):
                    _set_used(args, key, recipient, used_keys)

            # recipient_address passed as its own parameter (instead of
            # the LLM having to jam it into the body). Fan it out
            # alongside the contact-derived address fan-out above —
            # whichever path supplies the value, the template fills.
            if recipient_address:
                for key in ("recipient_address", "recipient_address_line1",
                             "empfaenger_adresse", "vermieter_adresse",
                             "anbieter_adresse", "locatore_indirizzo",
                             "conduttore_indirizzo", "sprzedawca_adres",
                             "wynajmujacy_adres"):
                    _set_used(args, key, recipient_address, used_keys)


            # Strip letter chrome from the LLM's body BEFORE stuffing it
            # into body_text so the template doesn't double-wrap chrome.
            sender_name = args.get("sender_name") or args.get("absender_name") or ""
            clean_body = _strip_letter_chrome(body_text, sender_name, recipient or "")
            # NB: paragraph-break already ran at the FIRST fan-out earlier
            # (line ~742). _set_used here is empty-only so re-running the
            # break at this point is a no-op — kept the strip for the
            # sender-name filtering only.
            if clean_body:
                for slot in ("body_text", "brief_text", "letter_text",
                              "treść", "testo", "texte"):
                    _set_used(args, slot, clean_body, used_keys)

            # Merge template defaults LAST — only fills slots that are
            # still empty. Anrede / Gruss / etc. land here unchanged.
            for k, v in (full_template.get("default_args") or {}).items():
                if args.get(k) in (None, ""):
                    args[k] = v
            # Drop polyglot/unused keys that got seeded into args via
            # earlier "always populate" paths but aren't referenced by
            # this template's body. Keeps the args panel readable.
            # Whitelist: keep recipient/subject (editor uses them),
            # numbering keys (might match by series), and any key the
            # template actually uses or has a default for.
            keep_anyway = {"recipient", "subject", "recipient_address",
                            "recipient_name"}
            template_default_keys = set((full_template.get("default_args") or {}).keys())
            args = {
                k: v for k, v in args.items()
                if (k in keep_anyway
                    or k in used_keys
                    or k in template_default_keys)
            }

            rendered = await _rdr.render_template(
                full_template, args, owner_user_id=user_id, fill_numbering=False,
            )
            rendered_body_html = rendered.get("html")
            if rendered.get("args"):
                args = rendered["args"]
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger("yorik.compose_draft").warning(
                "template render failed for %s: %s", template_id, exc,
            )

    # What we actually store: the rendered template HTML when a template
    # was applied, else the LLM's plain body wrapped in paragraphs.
    final_html = rendered_body_html or body_html

    # Coerce recipient to a string before persisting. The LLM occasionally
    # passes recipient as an integer (most commonly the contact_id),
    # which lands in compose_drafts.recipient (TEXT col) as a numeric
    # string OR a JSON int depending on Python's str()/json conversion
    # path — both forms then crash the chat-side InlineComposeDraft card
    # because RecipientInlinePicker calls .trim() on it. Force a string
    # at the seam so the SQL value is always a text-shaped recipient
    # name and the frontend can't trip again. None stays None.
    if recipient is not None and not isinstance(recipient, str):
        recipient = str(recipient)

    with conn_ctx(DEFAULT_DB_PATH) as conn:
        if existing_draft_id:
            # UPDATE the existing row in place — chat-driven tweaks stay
            # as a single draft instead of cluttering the sidebar with a
            # new one for every "ändere X auf Y" command.
            conn.execute(
                "UPDATE compose_drafts SET "
                "  kind=?, template_id=?, recipient=?, subject=?, body_html=?, "
                "  args_json=?, updated_at=datetime('now') "
                "WHERE id=?",
                (kind, template_id, recipient, subject, final_html,
                 json.dumps(args, default=str), int(existing_draft_id)),
            )
            draft_id = int(existing_draft_id)
            conn.commit()
        else:
            cur = conn.execute(
                "INSERT INTO compose_drafts "
                "(user_id, kind, template_id, recipient, subject, body_html, args_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, kind, template_id, recipient, subject, final_html,
                 json.dumps(args, default=str)),
            )
            draft_id = cur.lastrowid
            conn.commit()

    from backend.ui_tools import _append
    _append({
        "type":          "compose_draft_created",
        "draft_id":      draft_id,
        "kind":          kind,
        "recipient":     recipient,
        "subject":       subject,
        "template_id":   template_id,
        "template_name": (full_template or {}).get("name"),
        # Change-template UX moved to the picker flow (chat-side: ask Yorik
        # to suggest other templates; editor-side: template switcher).
        "alternates":    [],
        "preview":       _strip_html(body_html)[:160],
        # Hint to the card: which template slots are still empty. Frontend
        # shows them as "noch leer: X, Y — in Compose öffnen zum Befüllen".
        "missing_args":  missing_required_args,
    })

    _missing_hint = (
        f" Noch leere Pflichtfelder: {', '.join(missing_required_args)}. "
        "Im Compose-Editor füllen."
        if missing_required_args else ""
    )
    return {
        "_llm_hint": (
            f"shown_to_user:draft created with draft_id={draft_id}"
            + (f" using template '{full_template['name']}'" if full_template else "")
            + _missing_hint
            + f". For ANY follow-up edit in this conversation ('mach den Ton "
              f"freundlicher', 'ändere das Datum', etc.) call compose_draft "
              f"AGAIN with existing_draft_id={draft_id} — never start a new "
              f"draft for a refine. Acknowledge briefly in the user's language; "
              f"the user sees a card they can edit."
        ),
        "draft_id":      draft_id,
        "kind":          kind,
        "recipient":     recipient,
        "subject":       subject,
        "template_id":   template_id,
        "template_name": (full_template or {}).get("name"),
    }


def _strip_html(s: str) -> str:
    """Cheap tag-strip for the chat-card preview. Not security-sensitive
    (the body is from the LLM, not user-untrusted input) — just visual."""
    import re
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s
