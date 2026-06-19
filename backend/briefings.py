"""Briefing template loader + runner.

Templates live in `briefings/<id>/briefing.json` and are loaded at
app startup. Each template declares an ordered list of sections;
each section dispatches to a Skill (the skill-only escape hatch the
user picked over data_query). Optional `synthesize` block hands the
section results to qwen3 for a unified summary.

A future v2 will add per-user template overrides + a `data_query`
section type for direct SQLite access. The schema reserves a
`section.kind` discriminator so adding it later is non-breaking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("yorik.briefings")

BRIEFINGS_DIR = Path(__file__).resolve().parent.parent / "briefings"


@dataclass
class BriefingTemplate:
    id: str
    name: str
    description: str = ""
    author: str = ""
    version: str = "1.0"
    vertical: Optional[str] = None
    needs_apps: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    synthesize: dict = field(default_factory=dict)
    manifest_path: Optional[Path] = None

    def to_manifest(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "author": self.author, "version": self.version,
            "vertical": self.vertical, "needs_apps": self.needs_apps,
            "tags": self.tags, "sections": self.sections,
            "synthesize": self.synthesize,
        }


_registry: dict[str, BriefingTemplate] = {}


def load_all() -> dict[str, BriefingTemplate]:
    """Walk briefings/* and load every briefing.json. Idempotent."""
    global _registry
    _registry = {}
    if not BRIEFINGS_DIR.exists():
        BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
        return _registry
    for d in sorted(BRIEFINGS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        manifest = d / "briefing.json"
        if not manifest.exists():
            continue
        try:
            t = _load_one(manifest)
            _registry[t.id] = t
            log.info("registered briefing: %s (%s)", t.id, d.name)
        except Exception as e:
            log.exception("failed to load briefing %s: %s", d.name, e)
    log.info("briefings loaded: %d total", len(_registry))
    return _registry


def get_all() -> list[BriefingTemplate]:
    return list(_registry.values())


def get(tid: str) -> Optional[BriefingTemplate]:
    return _registry.get(tid)


def _load_one(path: Path) -> BriefingTemplate:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "id" not in raw or "name" not in raw or "sections" not in raw:
        raise ValueError("briefing.json missing id/name/sections")
    return BriefingTemplate(
        id=raw["id"],
        name=raw["name"],
        description=raw.get("description", ""),
        author=raw.get("author", ""),
        version=raw.get("version", "1.0"),
        vertical=raw.get("vertical"),
        needs_apps=raw.get("needs_apps") or [],
        tags=raw.get("tags") or [],
        sections=raw["sections"],
        synthesize=raw.get("synthesize") or {},
        manifest_path=path,
    )


# ───────────────────────── runner ──────────────────────────────────

async def run_briefing(template_id: str, user_id: str, role: str = "admin",
                        window_hours_override: Optional[int] = None,
                        for_date: Optional[str] = None) -> dict[str, Any]:
    """Execute a briefing template — dispatch every section via the
    skills registry, evaluate `condition`, optionally synthesize a
    summary on top.

    Returns a structured payload the UI renders:
      {
        template: {...manifest...},
        sections: [
          { id, title, icon, render, priority, ok: bool, result/error }
        ],
        synthesis: <text>  (only if template.synthesize.enabled)
        generated_at: <iso>
      }
    """
    t = get(template_id)
    if not t:
        return {"error": f"unknown briefing: {template_id}"}

    from .skills import get_registry, SkillContext
    reg = get_registry()
    ctx = SkillContext(reg, role=role, user_id=user_id)

    # Run sections in parallel where possible. Each is one skill call;
    # gather them so a slow LLM-backed section doesn't serialise the
    # whole briefing.
    async def _run_section(section: dict) -> dict:
        out = {
            "id":       section.get("id"),
            "title":    section.get("title"),
            "icon":     section.get("icon"),
            "render":   section.get("render", "raw_json"),
            "priority": section.get("priority", "normal"),
        }
        skill_name = section.get("skill")
        if not skill_name:
            out["ok"] = False
            out["error"] = "section has no skill (v1 supports skill-only)"
            return out
        if not reg.get(skill_name):
            out["ok"] = False
            out["error"] = f"unknown skill: {skill_name}"
            return out
        args = dict(section.get("args") or {})
        # Window override: if the template has time-based args we
        # rewrite them based on the user's window selection.
        if window_hours_override is not None:
            if "hours" in args:
                args["hours"] = window_hours_override
            if "days" in args:
                args["days"] = max(1, window_hours_override // 24)
        # Substitute time-aware template variables in every string arg
        # so a section can say `{"start_iso": "{today_start}"}` and get
        # the right ISO without the template author doing date math.
        # `for_date` reframes "today" to that date — used by snapshot
        # rendering ("show me day-recap as of 2026-05-19").
        args = _resolve_time_vars(args, for_date=for_date)
        try:
            result = await reg.invoke(skill_name, ctx=ctx, **args)
            # Evaluate condition (safe expression eval).
            cond = section.get("condition")
            if cond and not _eval_condition(cond, result):
                out["ok"] = True
                out["result"] = None  # signals "hide this section"
                out["hidden"] = True
                return out
            # Optional field_template + array unrolling for list-render.
            if out["render"] == "list" and section.get("field_template"):
                items = _coerce_to_array(result)
                lines = [_apply_template(section["field_template"], item)
                          for item in items]
                out["lines"] = lines
                out["raw"] = items
            out["ok"] = True
            out["result"] = result
            return out
        except Exception as e:
            log.exception("briefing section %s/%s failed: %s",
                          template_id, skill_name, e)
            out["ok"] = False
            out["error"] = str(e)
            return out

    section_results = await asyncio.gather(*[_run_section(s) for s in t.sections])

    payload: dict[str, Any] = {
        "template":   t.to_manifest(),
        "sections":   section_results,
        "generated_at": _now_iso(),
        "summary_line": _summary_line(section_results),
    }

    if t.synthesize and t.synthesize.get("enabled") and t.synthesize.get("prompt"):
        try:
            payload["synthesis"] = await _synthesize(t, section_results)
        except Exception as e:
            log.exception("briefing synthesis failed: %s", e)
            payload["synthesis_error"] = str(e)

    return payload


async def _synthesize(t: BriefingTemplate, sections: list[dict]) -> str:
    """Hand the section results to qwen3 with the template's prompt
    plus a uniform structured input so the LLM can reason across them."""
    from backend.whatsapp import _call_llm
    blocks = []
    for s in sections:
        if not s.get("ok") or s.get("hidden"):
            continue
        title = s.get("title") or s.get("id")
        r = s.get("result")
        if isinstance(r, dict):
            # Use the most useful field for the LLM context.
            txt = r.get("summary") or json.dumps(r, ensure_ascii=False)[:1500]
        elif isinstance(r, list):
            txt = "\n".join(json.dumps(i, ensure_ascii=False)[:200] for i in r[:10])
        else:
            txt = str(r)[:1500]
        blocks.append(f"## {title}\n{txt}")
    user_prompt = (
        t.synthesize.get("prompt", "Summarise the briefing sections below.")
        + "\n\n── Section results ──\n\n" + "\n\n".join(blocks)
    )
    return await _call_llm(user_prompt)


# ───────────────────────── condition + template eval ──────────────

# Tiny safe expression evaluator. Supports member access, comparison,
# AND/OR/NOT, parentheses, numeric/string literals. NO function calls,
# NO attribute access into unknown objects. The whitelist is small
# enough to audit by eye.

_SAFE_TOKEN = re.compile(r"""(
    \s+                          # whitespace
    | (?:result(?:\.[A-Za-z_][\w]*)*) # result.foo.bar
    | (?:and|or|not|True|False|None|isinstance|dict)
    | (?:[<>!=]=|<|>)             # comparisons
    | (?:[+\-*/])
    | \(|\)
    | (?:\d+(?:\.\d+)?)           # numbers
    | (?:"[^"]*"|'[^']*')          # strings
)""", re.VERBOSE)


def _eval_condition(expr: str, result: Any) -> bool:
    """Evaluates expressions like `result.stats.unread_count > 0`.
    Tokenises against a strict whitelist; anything unrecognised is
    rejected (returns True so the section still shows — fail-open
    is friendlier than silently hiding everything)."""
    try:
        # Walk the string, ensuring every non-whitespace chunk is in
        # the whitelist. Then transpile the dotted access to dict
        # indexing and exec in a sealed dict-only namespace.
        i = 0
        while i < len(expr):
            m = _SAFE_TOKEN.match(expr, i)
            if not m:
                return True  # fail-open
            i = m.end()
        # Replace `result.a.b` with safe dict access via getattr-style.
        safe = re.sub(r"result((?:\.[A-Za-z_][\w]*)+)",
                       lambda m: _to_dict_access(m.group(1)),
                       expr)
        # eval in a sealed namespace. Allow `isinstance` + `dict` so the
        # `_to_dict_access` transpilation works — without these the
        # eval raises NameError and the fail-open path silently returns
        # True for every condition (bug discovered when Bills section
        # ignored `result.count > 0` and rendered with an empty list).
        return bool(eval(safe, {"__builtins__": {}},
                         {"_R": result, "isinstance": isinstance, "dict": dict}))
    except Exception:
        return True


def _to_dict_access(dotted: str) -> str:
    # ".a.b.c"  →  "_R.get('a', {}).get('b', {}).get('c', None) if isinstance(_R, dict) else None"
    parts = [p for p in dotted.split(".") if p]
    expr = "_R"
    for p in parts:
        expr = f"(({expr}).get('{p}', None) if isinstance({expr}, dict) else None)"
    return expr


_TEMPLATE_VAR = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _apply_template(tpl: str, item: dict) -> str:
    """Tiny mustache subset: {{ foo }} and {{ foo.bar }}."""
    if not isinstance(item, dict):
        item = {"value": item}
    def repl(m):
        path = m.group(1).split(".")
        cur: Any = item
        for p in path:
            if isinstance(cur, dict):
                cur = cur.get(p, "")
            else:
                cur = ""
                break
        return str(cur) if cur is not None else ""
    return _TEMPLATE_VAR.sub(repl, tpl)


def _coerce_to_array(result: Any) -> list:
    """Most skills return {events: [...], hits: [...], drafts: [...]}.
    Find the first array-valued top-level key and use it; otherwise
    return the raw value wrapped."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for v in result.values():
            if isinstance(v, list):
                return v
    return [result] if result else []


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _summary_line(sections: list[dict]) -> str:
    """One-line scannable summary, composed from section payloads.

    Pulls counts straight from the standardised result shapes the
    new check_* skills return (events / tasks / bills / photos). Order
    chosen so time-bound things come first (events > tasks > bills),
    photos last (informational, not actionable). Empty briefings get
    a friendly fallback so the line is never blank.

    Kept English-only for now to match the rest of the briefing chrome
    — a localised pass can mirror the dashboard_routes labels later.
    """
    parts: list[str] = []
    for s in sections or []:
        if not s.get("ok") or s.get("hidden"):
            continue
        r = s.get("result") or {}
        events = r.get("events")
        if isinstance(events, list) and events:
            parts.append(f"{len(events)} event{'s' if len(events) != 1 else ''}")
            continue
        tasks = r.get("tasks")
        if isinstance(tasks, list) and tasks:
            open_n = sum(1 for t in tasks if not t.get("done"))
            done_n = len(tasks) - open_n
            if open_n and done_n:
                parts.append(f"{open_n} open + {done_n} done task{'s' if (open_n+done_n) != 1 else ''}")
            elif open_n:
                parts.append(f"{open_n} task{'s' if open_n != 1 else ''}")
            elif done_n:
                parts.append(f"{done_n} task{'s' if done_n != 1 else ''} done")
            continue
        bills = r.get("bills")
        if isinstance(bills, list) and bills:
            parts.append(f"{len(bills)} bill{'s' if len(bills) != 1 else ''} due")
            continue
        photos = r.get("photos")
        if isinstance(photos, list) and photos:
            parts.append(f"{len(photos)} photo{'s' if len(photos) != 1 else ''}")
            continue
        # Email/whatsapp summaries return a `summary` markdown — no
        # count, but presence is signal. Skip if empty.
        if isinstance(r.get("summary"), str) and r["summary"].strip():
            # We don't have a count for unstructured text; just hint
            # at activity for the most common section ids.
            sid = (s.get("id") or "").lower()
            if "email" in sid: parts.append("email digest ready")
            elif "whatsapp" in sid: parts.append("WhatsApp digest ready")
    if not parts:
        return "Nothing to brief — all clear."
    return ", ".join(parts) + "."


def _resolve_time_vars(args: dict, for_date: Optional[str] = None) -> dict:
    """Substitute time-aware template strings in section args.

    Lets briefing templates write declarative SQL/skill calls like:
        {"start_iso": "{today_start}", "end_iso": "{today_end}"}
    instead of forcing every template author to do datetime math.

    `for_date` reframes "today" to that ISO date — used by snapshot
    rendering. When set:
      - {today_*}     refers to for_date (not date.today())
      - {yesterday_*} = for_date − 1
      - {tomorrow_*}  = for_date + 1
      - {now}         = end-of-day for that date (the day is "over")

    Vocabulary (all resolve to local-time ISO strings):
        {today_start}       midnight today (or for_date)
        {today_end}         23:59:59 today (or for_date)
        {yesterday_start}   midnight day before
        {yesterday_end}     23:59:59 day before
        {tomorrow_start}    midnight day after
        {tomorrow_end}      23:59:59 day after
        {now}               current timestamp (or for_date 23:59:59)
        {today_date}        YYYY-MM-DD
        {yesterday_date}    YYYY-MM-DD
        {tomorrow_date}     YYYY-MM-DD

    Substitutes only on string values — leaves ints, bools, dicts
    alone. No recursion into nested dicts/lists (no current template
    needs it; trivial to add later).
    """
    from datetime import datetime, date, time, timedelta
    if for_date:
        try:
            today = date.fromisoformat(for_date)
        except ValueError:
            today = date.today()
    else:
        today = date.today()
    yest = today - timedelta(days=1)
    tom = today + timedelta(days=1)

    def _iso_start(d: date) -> str:
        return datetime.combine(d, time.min).isoformat(timespec="seconds")

    def _iso_end(d: date) -> str:
        return datetime.combine(d, time.max).isoformat(timespec="seconds")

    now_iso = (_iso_end(today) if for_date
               else datetime.now().isoformat(timespec="seconds"))
    vocab = {
        "{today_start}":     _iso_start(today),
        "{today_end}":       _iso_end(today),
        "{yesterday_start}": _iso_start(yest),
        "{yesterday_end}":   _iso_end(yest),
        "{tomorrow_start}":  _iso_start(tom),
        "{tomorrow_end}":    _iso_end(tom),
        "{now}":             now_iso,
        "{today_date}":      today.isoformat(),
        "{yesterday_date}":  yest.isoformat(),
        "{tomorrow_date}":   tom.isoformat(),
    }

    out: dict = {}
    for k, v in args.items():
        if isinstance(v, str):
            for marker, replacement in vocab.items():
                if marker in v:
                    v = v.replace(marker, replacement)
        out[k] = v
    return out
