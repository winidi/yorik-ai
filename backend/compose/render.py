"""Template + data → HTML.

1. Pull data from each connector op listed in template.data_query
2. Run Jinja2 over template.body_html with the merged data context
3. Return the rendered HTML (and the raw data for the AI panel)

Jinja2 is fed a small set of filters every German business document needs:
  euro      — 1840 → "1.840,00 €"
  date_de   — ISO → "12.04.2026"
  upper_de  — uppercase that preserves German umlauts
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Any, Dict, List, Optional

from jinja2 import ChainableUndefined, Environment, select_autoescape

log = logging.getLogger("homeos.compose.render")


# ─── Filters ──────────────────────────────────────────────────────────────

def euro(value: Any) -> str:
    """Format a number as a German EUR amount: '1.840,00 €'."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    s = f"{n:,.2f}"
    # Swap , and . to German order: 1,234.56 → 1.234,56
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    return f"{s} €"


def date_de(value: Any, fmt: str = "%d.%m.%Y") -> str:
    """ISO date string (or datetime) → German DD.MM.YYYY."""
    if value is None or value == "":
        return ""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime(fmt)
    try:
        d = dt.datetime.fromisoformat(str(value).replace("Z", ""))
        return d.strftime(fmt)
    except ValueError:
        return str(value)


def today_de(fmt: str = "%d.%m.%Y") -> str:
    return dt.date.today().strftime(fmt)


# ─── Data resolution ──────────────────────────────────────────────────────

async def _resolve_data(data_query: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Run each declared connector op, return {key: result} for templating.

    data_query is a dict like:
        { "patient": { "op": "praxis.patient_akte",
                       "args": { "patient_id": "$arg.patient_id" } } }

    Values prefixed with '$arg.' are pulled from the user-supplied args dict.
    Empty or unresolvable ops surface as None so templates can `{% if patient %}`.
    """
    from .. import connectors as connectors_mod

    out: Dict[str, Any] = {}
    for key, spec in (data_query or {}).items():
        if not isinstance(spec, dict) or "op" not in spec:
            out[key] = None
            continue
        op_name = spec["op"]
        op_args = {}
        for k, v in (spec.get("args") or {}).items():
            if isinstance(v, str) and v.startswith("$arg."):
                op_args[k] = args.get(v[len("$arg."):])
            else:
                op_args[k] = v
        try:
            result = await connectors_mod.invoke(op_name, op_args)
            # Unwrap {"value": [...]} list-wrappers so templates can iterate cleanly.
            if isinstance(result, dict) and set(result.keys()) <= {"value", "ok"} and "value" in result:
                result = result["value"]
            out[key] = result
        except Exception as exc:  # noqa: BLE001
            log.warning("compose: data_query for %s failed: %s", key, exc)
            out[key] = None
    return out


# ─── Rendering ────────────────────────────────────────────────────────────

def _env() -> Environment:
    env = Environment(
        autoescape=select_autoescape(enabled_extensions=("html",), default=True),
        # Don't fail hard on missing vars — show empty so templates remain
        # editable even when partial data is available. ChainableUndefined
        # lets {{ akte.patient.nachname }} silently return "" if any link
        # in the chain is missing instead of raising.
        undefined=ChainableUndefined,
        keep_trailing_newline=True,
    )
    env.filters["euro"] = euro
    env.filters["date_de"] = date_de
    env.globals["today_de"] = today_de
    return env


async def render_template(
    template: Dict[str, Any],
    args: Dict[str, Any],
    *,
    owner_user_id: Optional[int] = None,
    fill_numbering: bool = True,
    use_preview_args: bool = False,
) -> Dict[str, Any]:
    """Resolve data_query, run Jinja over body_html, return {html, data, numbering}.

    `numbering` is a dict of {arg_key → preview} for any arg whose key
    matches a known numbering convention (e.g. `rechnungsnummer` →
    rechnung series). The previews are non-destructive — calling
    render_template a hundred times does NOT consume series numbers.
    The Save/Send endpoints call `compose.series.consume()` exactly
    once at allocation time.

    Templates can opt out of auto-numbering by passing
    `fill_numbering=False` (e.g. when re-rendering with a number the
    user has manually overridden in the args panel).
    """
    args = dict(args or {})
    numbering_previews: Dict[str, Any] = {}
    if fill_numbering:
        # Lazy import — series module touches the DB and we don't want
        # render.py to crash just because the table isn't there yet.
        try:
            from . import series as ser
            for key in list(args.keys()):
                kind = ser.kind_for_arg_key(key)
                if not kind:
                    continue
                preview = ser.preview_next_for_kind(kind, owner_user_id=owner_user_id)
                if not preview:
                    continue
                # Only override if the current arg value looks like a
                # placeholder (the default_args sentinel) rather than
                # something the user typed. Heuristic: empty, or starts
                # with "PLACEHOLDER", or is the literal default value
                # that came from the template. We use a string-equality
                # check against the original default; that comes via the
                # caller passing in default_args unchanged on the first
                # draft. To stay safe, we ALWAYS preview but only auto-
                # fill on the FIRST render (when the arg value matches
                # the template's default_args[key]).
                tpl_default = (template.get("default_args") or {}).get(key)
                if args.get(key) == tpl_default:
                    args[key] = preview["formatted"]
                numbering_previews[key] = {
                    "series_id": preview["series_id"],
                    "kind": kind,
                    "formatted": preview["formatted"],
                    "year": preview["year"],
                    "number": preview["number"],
                }
        except Exception as exc:  # noqa: BLE001
            log.warning("compose: numbering preview skipped: %s", exc)

    data = await _resolve_data(template.get("data_query") or {}, args or {})
    env = _env()

    # Build render_args from layered fallbacks. The user's `args` is
    # returned UNCHANGED — only `render_args` (what Jinja sees) gets
    # the overlay, so the editor's input fields stay empty for the user
    # to fill.
    #
    # Priority order (highest wins):
    #   1. user-supplied args
    #   2. preview_args     ← only when use_preview_args (editor preview)
    #   3. template.default_args  ← always; carries non-placeholder text
    #                              like "in der letzten Woche…" that the
    #                              template author meant as the real
    #                              starting copy
    #
    # compose_draft (LLM/skill path) passes use_preview_args=False and
    # also pre-merges default_args itself, so this layering is a no-op
    # for real-draft creation.
    render_args = dict(args or {})
    if use_preview_args:
        for k, v in (template.get("preview_args") or {}).items():
            if render_args.get(k) in (None, ""):
                render_args[k] = v
    for k, v in (template.get("default_args") or {}).items():
        if render_args.get(k) in (None, ""):
            render_args[k] = v

    try:
        tmpl = env.from_string(template["body_html"])
        html = tmpl.render(args=render_args, **data)
    except Exception as exc:  # noqa: BLE001
        log.exception("compose: template %s render failed: %s", template.get("id"), exc)
        html = f"<p><strong>Render error:</strong> {exc}</p>"
    return {"html": html, "data": data, "numbering": numbering_previews, "args": args}
