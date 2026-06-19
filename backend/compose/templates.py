"""Template registry — loads + validates JSON templates from templates/.

Each template is a small declarative spec:
  id, name, description, vertical, needs_apps   – metadata
  data_query                                   – which connector ops to call
  body_html                                    – Jinja2-templated HTML payload
  page                                         – PDF page setup hints

The registry scans templates/ at startup AND on every list call (cheap).
That way a maintainer can drop a new template JSON in and see it without
restarting Yorik. Tier-0 schema validation rejects malformed files.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("homeos.compose.templates")

TEMPLATES_DIR = Path(os.getenv("HOMEOS_TEMPLATES_DIR", "templates"))

# Required fields per template. Tier-0: just structural validity, not deep
# semantic checks. The render pipeline will surface deeper issues at use time.
REQUIRED_FIELDS = {"id", "name", "version", "body_html"}
OPTIONAL_FIELDS = {
    "description", "author", "license", "vertical", "needs_apps",
    "data_query", "page", "llm_hints", "css", "tags", "default_args",
    # IETF BCP 47 language tag — drives the LLM's reply language via
    # the loop._enrich_template_picked language-pin path. `en`,
    # `en-US`, `de`, `de-DE` etc. Absent on community templates that
    # don't care.
    "locale",
    # ZUGFeRD / Factur-X opt-in. When `zugferd` is true the template MUST
    # also provide `invoice_fields` mapping the structured payload the
    # XML generator wants (lives in extensions/zugferd/).
    "zugferd", "invoice_fields",
    # Optional list of extension ids the template requires (e.g. ["zugferd"]).
    # The Compose template picker surfaces a "needs Extension X — Install"
    # link when any are missing, so the user gets a clear path.
    "requires_extensions",
    # Skills metadata: declares which skills can auto-fill / export / send /
    # voice-trigger this template, the template's language, and whether it
    # cares about the calendar. Used by the skills registry and the chat
    # agent's open_app + use_skill dispatch. Tier-0 validator just accepts
    # the field — deeper schema lives alongside the skills registry.
    "skills_supported",
    # editor_notes — usage hints / legal-context disclaimers / "send via
    # Einschreiben" reminders. Rendered by ComposeApp as a card BELOW
    # the editor — NEVER goes into the body_html and therefore never
    # into the rendered PDF. Light HTML (<p>, <strong>, <em>, <br>) ok.
    "editor_notes",
    # preview_args — example values shown ONLY in the empty-state editor
    # preview so users browsing a template see what a filled letter looks
    # like (e.g. "Max Mustermann / Musterstraße 1, 12345 Musterstadt").
    # compose_draft (the LLM/skill path that creates real drafts) NEVER
    # touches these — real data always comes from contacts + profile +
    # explicit args. /api/compose/draft (the editor preview endpoint)
    # falls back to preview_args[k] for any empty arg key so the visual
    # preview is never broken.
    "preview_args",
    # ask_user_for_args — list of {key, label, required, pattern?, hint?}
    # consumed by the compose_check_template_args skill. Lets the template
    # author polish the labels + required-flags for fields the user must
    # provide (Mietvertrag-Datum, Wohnungsadresse, Vertragsnummer …).
    # Optional — when absent, the skill auto-detects from body_html.
    "ask_user_for_args",
    # delivery_default — "attachment" (PDF attached to email; default for
    # formal letters / Kündigungen / Rechnungen where DIN 5008 layout
    # matters) OR "inline" (HTML body rendered as the email body itself;
    # default for short informal emails / quick replies). The SendDialog
    # uses this to pre-select the radio; the user can override per send.
    "delivery_default",
    # subject_template — small Jinja string rendered server-side when
    # the LLM didn't pass a subject and the user hasn't typed one. Saves
    # the LLM from guessing a subject line for every Kündigung / Rechnung
    # / letter. Has access to `args.X` (same args dict the body sees).
    # Example: "Kündigung Mietverhältnis – {{ args.wohnung_adresse }}"
    "subject_template",
    # kind — one of letter/invoice/offer/email/memo. The chat card's
    # header label + icon read from this (📄 Brief vs 📧 E-Mail). When
    # absent, compose_draft falls back to the LLM-supplied kind. Each
    # template knows what it is, so the skill doesn't have to guess
    # from tags / name. Templates SHOULD set this; absent is tolerated
    # for back-compat with older author-shipped templates.
    "kind",
    # when_to_use / when_to_not_use — Hermes-style picking grounding,
    # same shape as skill manifests. Lists of short bullets; the picker
    # convention is to keep them SAME bullet count and roughly equal
    # total length so the LLM doesn't bias by mass. list_compose_templates
    # scores positively against when_to_use and negatively against
    # when_to_not_use. Each "don't use" bullet should name the nearest
    # neighbour to redirect to (e.g. "User says 'E-Mail' — use
    # generic-email."). Absence is tolerated for back-compat; templates
    # that omit them fall through to name/description scoring only.
    "when_to_use", "when_to_not_use",
}


class TemplateError(ValueError):
    pass


def _validate(t: Dict[str, Any], source: str) -> List[str]:
    errs: List[str] = []
    missing = REQUIRED_FIELDS - set(t)
    if missing:
        errs.append(f"missing required fields: {sorted(missing)}")
    unknown = set(t) - REQUIRED_FIELDS - OPTIONAL_FIELDS
    if unknown:
        errs.append(f"unknown fields: {sorted(unknown)}")
    aid = t.get("id", "")
    if not isinstance(aid, str) or not aid or not aid.replace("-", "_").replace("_", "").isalnum():
        errs.append("id must be alphanumeric + hyphens/underscores")
    dq = t.get("data_query") or {}
    if not isinstance(dq, dict):
        errs.append("data_query must be a dict")
    nq = t.get("needs_apps") or []
    if not isinstance(nq, list) or not all(isinstance(x, str) for x in nq):
        errs.append("needs_apps must be a list of strings")
    return errs


def load_all() -> List[Dict[str, Any]]:
    """Scan TEMPLATES_DIR and return every valid template. Malformed files
    are logged and skipped so one bad JSON doesn't kill the whole list."""
    if not TEMPLATES_DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                t = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("template %s unreadable: %s", p.name, exc)
            continue
        errs = _validate(t, p.name)
        if errs:
            log.warning("template %s invalid: %s", p.name, errs)
            continue
        # Tag the source path so the marketplace tab can surface it.
        t["_source_path"] = str(p)
        out.append(t)
    return out


def get(template_id: str) -> Dict[str, Any]:
    """Fetch one template by id. Raises TemplateError if not found."""
    for t in load_all():
        if t.get("id") == template_id:
            return t
    raise TemplateError(f"template not found: {template_id}")


def public_dict(t: Dict[str, Any]) -> Dict[str, Any]:
    """Slim shape returned by /api/compose/templates and embedded in the
    /api/compose/draft response.

    Includes the full body_html — the frontend ArgsList needs it to
    classify which args actually alter the rendered letter (referenced
    via `args.<key>`) versus which are routing metadata (recipient
    email, subject for an email kind, …) that should live below the
    hairline in the right-pane args list. Templates are 1–20 KB each
    and the list view fetches once per session, so the wire-size cost
    is acceptable for a feature that needs to work across every
    template without per-template plumbing."""
    return {
        "id": t["id"],
        "name": t["name"],
        "description": t.get("description") or "",
        "version": t.get("version") or "1.0",
        "author": t.get("author") or "yorik-core",
        "vertical": t.get("vertical"),
        "needs_apps": list(t.get("needs_apps") or []),
        "requires_extensions": list(t.get("requires_extensions") or []),
        "tags": list(t.get("tags") or []),
        "default_args": dict(t.get("default_args") or {}),
        "body_html": t.get("body_html") or "",
        # editor_notes — usage hints / legal-context disclaimers that live
        # OUTSIDE the editable body. ComposeApp renders this as a card
        # below the editor. NEVER goes into the rendered PDF. Light HTML
        # is allowed (<strong>, <em>, <br>, <p>); the field is template-
        # author trusted (templates ship in-tree or via the curated hub).
        "editor_notes": t.get("editor_notes") or "",
        # preview_args — example values used by the editor's empty-state
        # preview so users see a realistic letter shape, not blank slots.
        # NOT a default for real drafts (compose_draft skill ignores it).
        "preview_args": dict(t.get("preview_args") or {}),
        # ask_user_for_args — surfaced so the frontend could (later) render
        # progressive disclosure / explain why a field is required. The
        # compose_check_template_args skill is the primary consumer.
        "ask_user_for_args": list(t.get("ask_user_for_args") or []),
        # delivery_default — "attachment" or "inline"; consumed by the
        # SendDialog to pre-select the delivery radio. None when the
        # template doesn't declare a preference (SendDialog falls back
        # to "attachment" — formal-letter default).
        "delivery_default": t.get("delivery_default"),
        # kind — letter/email/invoice/offer/memo. Drives the chat card's
        # header label + icon. Authoring rule: every template should
        # declare it.
        "kind": t.get("kind"),
        # Picker grounding (mig: templates-as-skills). Surfaced to the
        # LLM by list_compose_templates so picking quality matches the
        # skill-picking quality. Authoring rule: same bullet count,
        # similar total length per template.
        "when_to_use":     list(t.get("when_to_use") or []),
        "when_to_not_use": list(t.get("when_to_not_use") or []),
    }
