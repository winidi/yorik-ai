#!/usr/bin/env python3
"""Voice-command eval harness — sends text commands to /api/ask and
asserts side effects.

Skips Whisper STT (deterministic per audio file) and tests the
interesting failure surface: prompt + LLM + skill dispatch. Each case
seeds the DB, runs 2-3 paraphrases, and checks one of:

  - db_state          — a SQL query returns the expected row(s)
  - response_contains — the response text contains any of N substrings
  - ui_action_emitted — a ui_action of given type was returned
  - skill_invoked     — sql_used / debug surface mentions a skill

A case passes only when ALL variants pass — robustness across phrasing
is the point.

USAGE
-----
  scripts/voice_eval.py                                # all cases, default LLM
  scripts/voice_eval.py --filter reschedule            # only id matches
  scripts/voice_eval.py --tag calendar                 # only those tagged
  scripts/voice_eval.py --llm-model qwen3.6-27b-mtp    # try a different LLM
                                                         (must already be loaded
                                                         in your llama-swap)
  scripts/voice_eval.py --json out.json --md report.md  # save artifacts

REPORT
------
Pass-rate per case, side-by-side variants, time per call. Saved as
JSON for diffing across runs and as a Markdown table for humans.

NOTES
-----
- This is a dev tool. Runs against the live DB — don't point it at
  production. Use a `cp data/family.db data/family.eval.db` and set
  HOMEOS_DB_PATH first if you care.
- Each case includes a setup_sql that creates the seed row needed; we
  do NOT roll back after the run, so the DB may end in a modified
  state. Idempotent setup_sql (DELETE then INSERT) makes re-runs safe.
- {tomorrow}, {day_after_tomorrow}, {today}, {yesterday} placeholders
  in `must_match` values resolve to YYYY-MM-DD before comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

# Default endpoints; override via flags or env.
DEFAULT_API_BASE = os.getenv("YORIK_EVAL_API_BASE", "http://localhost:8000")
DEFAULT_DB_PATH = os.getenv("HOMEOS_DB_PATH",
                            str(Path(__file__).parent.parent / "data" / "family.db"))


# ─── data shapes ────────────────────────────────────────────────────


@dataclass
class VariantResult:
    variant: str
    ok: bool
    detail: str
    duration_s: float
    response: str
    sql_used: Optional[str] = None
    ui_actions: list = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    tags: list[str]
    variants: list[VariantResult]
    @property
    def pass_count(self) -> int:
        return sum(1 for v in self.variants if v.ok)
    @property
    def total(self) -> int:
        return len(self.variants)
    @property
    def overall_pass(self) -> bool:
        return self.pass_count == self.total


# ─── helpers ────────────────────────────────────────────────────────


def _resolve_placeholders(value: Any) -> Any:
    """Substitute {tomorrow}, {today}, etc. in string values."""
    if not isinstance(value, str):
        return value
    today = date.today()
    repl = {
        "{today}":               today.isoformat(),
        "{tomorrow}":            (today + timedelta(days=1)).isoformat(),
        "{day_after_tomorrow}":  (today + timedelta(days=2)).isoformat(),
        "{yesterday}":           (today - timedelta(days=1)).isoformat(),
    }
    out = value
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def _get_session_cookie(db_path: str) -> str:
    """Pull the most recent live admin session cookie from the DB —
    saves us implementing a login flow in the eval tool."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT s.id FROM sessions s "
        "JOIN user_profiles u ON u.id = s.user_id "
        "WHERE u.role = 'admin' AND s.expires_at > datetime('now') "
        "ORDER BY s.last_seen_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        raise SystemExit(
            "no active admin session in the DB. Log in once via the browser "
            "(http://localhost:8000) so we can reuse the cookie."
        )
    return row[0]


def _setup_case(db_path: str, sql: str) -> None:
    if not sql or not sql.strip():
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def _run_query(db_path: str, sql: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _assert_expected(expected: dict, db_path: str, response: str,
                     sql_used: Optional[str], ui_actions: list) -> tuple[bool, str]:
    """Returns (ok, human-readable detail)."""
    etype = expected.get("type")

    if etype == "db_state":
        query = expected["query"]
        must = {k: _resolve_placeholders(v) for k, v in (expected.get("must_match") or {}).items()}
        rows = _run_query(db_path, query)
        if not rows:
            return False, f"no rows returned by check query"
        row = rows[0]
        misses = []
        for k, expected_val in must.items():
            actual = row.get(k)
            if str(actual) != str(expected_val):
                misses.append(f"{k}: want {expected_val!r}, got {actual!r}")
        if misses:
            return False, "; ".join(misses)
        return True, "db state matches"

    if etype == "response_contains":
        needles = expected.get("any_of") or [expected.get("substr", "")]
        body = (response or "").lower()
        hits = [n for n in needles if n and n.lower() in body]
        if hits:
            return True, f"hit: {hits[0]!r}"
        return False, f"none of {needles} in response: {response[:80]!r}"

    if etype == "ui_action_emitted":
        target = expected["of_type"]
        for a in ui_actions or []:
            if isinstance(a, dict) and a.get("type") == target:
                return True, f"ui_action {target!r} emitted"
        types = [a.get("type") for a in (ui_actions or []) if isinstance(a, dict)]
        return False, f"no {target!r} action; got: {types}"

    if etype == "skill_invoked":
        hint = expected.get("skill_hint", "")
        haystack = ((sql_used or "") + " " + (response or "")).lower()
        if hint and hint.lower() in haystack:
            return True, f"skill hint {hint!r} surfaced"
        # Fall back: check if ui_actions hint at the right action.
        for a in ui_actions or []:
            if isinstance(a, dict) and hint.replace("_", "").lower() in (a.get("type") or "").replace("_", "").lower():
                return True, f"ui_action implies {hint!r}"
        return False, f"no sign of {hint!r} in response/sql/ui_actions"

    return False, f"unknown expected.type: {etype!r}"


# ─── runner ─────────────────────────────────────────────────────────


def _send_ask(api_base: str, cookie: str, message: str, timeout: float = 60.0) -> dict:
    import requests
    r = requests.post(
        f"{api_base}/api/ask",
        json={"message": message},
        cookies={"yorik_session": cookie},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def run_case(case: dict, api_base: str, cookie: str, db_path: str,
             delay_s: float = 0.0) -> CaseResult:
    variants = []
    for i, v in enumerate(case.get("variants") or []):
        if delay_s > 0 and i > 0:
            time.sleep(delay_s)
        _setup_case(db_path, case.get("setup_sql", ""))
        t0 = time.time()
        try:
            resp = _send_ask(api_base, cookie, v)
            elapsed = time.time() - t0
            ok, detail = _assert_expected(
                case["expected"], db_path,
                response=resp.get("response", ""),
                sql_used=resp.get("sql_used"),
                ui_actions=resp.get("ui_actions") or [],
            )
            variants.append(VariantResult(
                variant=v, ok=ok, detail=detail,
                duration_s=round(elapsed, 2),
                response=(resp.get("response") or "")[:200],
                sql_used=resp.get("sql_used"),
                ui_actions=resp.get("ui_actions") or [],
            ))
        except Exception as e:
            variants.append(VariantResult(
                variant=v, ok=False, detail=f"exception: {type(e).__name__}: {e}",
                duration_s=round(time.time() - t0, 2), response="",
            ))
    return CaseResult(case_id=case["id"], tags=case.get("tags") or [],
                      variants=variants)


# ─── reporting ──────────────────────────────────────────────────────


GREEN = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"
DIM = "\033[2m"; RESET = "\033[0m"


def _fmt_console(results: list[CaseResult]) -> None:
    tty = sys.stdout.isatty()
    def c(color, s): return f"{color}{s}{RESET}" if tty else s

    total_v = sum(r.total for r in results)
    pass_v = sum(r.pass_count for r in results)
    pct = (pass_v / total_v * 100) if total_v else 0

    print(f"\n{'='*70}")
    print(f"VOICE EVAL — {pass_v}/{total_v} variants passed ({pct:.0f}%)")
    print(f"{'='*70}\n")

    for r in results:
        status = c(GREEN, "✓") if r.overall_pass else (
            c(YELLOW, "·") if r.pass_count > 0 else c(RED, "✗"))
        print(f"{status} {r.case_id} ({r.pass_count}/{r.total}) "
              f"[{','.join(r.tags)}]")
        for v in r.variants:
            mark = c(GREEN, " ✓") if v.ok else c(RED, " ✗")
            avg = f"{v.duration_s}s"
            print(f"  {mark} {v.variant[:75]:75} {avg:>7}")
            if not v.ok:
                print(c(DIM, f"      → {v.detail}"))
                if v.response:
                    print(c(DIM, f"      ← {v.response[:120]}"))
        print()


def _write_json(results: list[CaseResult], path: str, meta: dict) -> None:
    payload = {
        "meta": meta,
        "summary": {
            "total_cases": len(results),
            "passing_cases": sum(1 for r in results if r.overall_pass),
            "total_variants": sum(r.total for r in results),
            "passing_variants": sum(r.pass_count for r in results),
        },
        "cases": [
            {
                "id": r.case_id,
                "tags": r.tags,
                "pass": r.overall_pass,
                "variants": [
                    {
                        "variant": v.variant, "ok": v.ok,
                        "detail": v.detail, "duration_s": v.duration_s,
                        "response": v.response, "sql_used": v.sql_used,
                    } for v in r.variants
                ],
            } for r in results
        ],
    }
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"  → JSON written to {path}")


def _write_markdown(results: list[CaseResult], path: str, meta: dict) -> None:
    total_v = sum(r.total for r in results)
    pass_v = sum(r.pass_count for r in results)
    pct = (pass_v / total_v * 100) if total_v else 0
    lines = [
        f"# Voice eval report",
        "",
        f"- **Model:** `{meta.get('llm_model', '?')}`",
        f"- **Endpoint:** `{meta.get('api_base')}`",
        f"- **Run at:** {meta.get('started_at')}",
        f"- **Overall:** {pass_v}/{total_v} variants ({pct:.0f}%)",
        "",
        "## Per-case results",
        "",
        "| Case | Tags | Pass | Variants OK | Notes |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        status = "✅" if r.overall_pass else ("⚠️" if r.pass_count > 0 else "❌")
        first_fail = next((v for v in r.variants if not v.ok), None)
        notes = first_fail.detail if first_fail else ""
        lines.append(f"| `{r.case_id}` | {','.join(r.tags)} | {status} | {r.pass_count}/{r.total} | {notes[:80]} |")
    lines.append("")
    lines.append("## Failing variants — full detail")
    lines.append("")
    for r in results:
        for v in r.variants:
            if v.ok: continue
            lines.append(f"### `{r.case_id}` — {v.variant!r}")
            lines.append(f"- **Why it failed:** {v.detail}")
            lines.append(f"- **Response (first 200 chars):** `{v.response}`")
            if v.sql_used:
                lines.append(f"- **SQL the agent ran:** `{v.sql_used}`")
            lines.append("")
    Path(path).write_text("\n".join(lines))
    print(f"  → Markdown written to {path}")


# ─── CLI ────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases-file",
                    default=str(Path(__file__).parent / "voice_eval_cases.yaml"),
                    help="YAML cases file (default: scripts/voice_eval_cases.yaml)")
    ap.add_argument("--api-base", default=DEFAULT_API_BASE,
                    help="Yorik API base URL")
    ap.add_argument("--db-path", default=DEFAULT_DB_PATH,
                    help="SQLite path for setup_sql + assertions")
    ap.add_argument("--filter", help="Substring match on case id")
    ap.add_argument("--tag", help="Only cases with this tag")
    ap.add_argument("--json", help="Write JSON report to this path")
    ap.add_argument("--md", help="Write Markdown report to this path")
    ap.add_argument("--llm-model",
                    help="Informational only — sets the meta header. The actual "
                         "LLM is whatever Yorik is configured to use.")
    ap.add_argument("--delay", type=float, default=4.5,
                    help="Seconds to sleep between requests. Yorik's API rate "
                         "limit on /api/ask is 15/min (= one per 4s), so this "
                         "defaults to 4.5s. Set 0 only if you've raised the limit.")
    ap.add_argument("--reset-cache", action="store_true",
                    help="Wipe saved_queries before running. Removes the "
                         "/api/ask phrase cache so each variant exercises the "
                         "live LLM path. Required for trustworthy results "
                         "since a poisoned cache row can replay a stale answer.")
    args = ap.parse_args()

    if args.reset_cache:
        import sqlite3
        with sqlite3.connect(args.db_path) as _c:
            n = _c.execute("DELETE FROM saved_queries").rowcount
            _c.commit()
        print(f"[reset-cache] wiped {n} saved_queries row(s)")

    cases_path = Path(args.cases_file)
    if not cases_path.exists():
        return _fail(f"cases file not found: {cases_path}")

    cases_data = yaml.safe_load(cases_path.read_text()) or {}
    cases = cases_data.get("cases") or []
    if args.filter:
        cases = [c for c in cases if args.filter in c.get("id", "")]
    if args.tag:
        cases = [c for c in cases if args.tag in (c.get("tags") or [])]
    if not cases:
        return _fail("no cases matched filter/tag")

    cookie = _get_session_cookie(args.db_path)

    from datetime import datetime
    meta = {
        "api_base": args.api_base,
        "llm_model": args.llm_model or os.getenv("HOMEOS_MODEL", "(unset)"),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "case_count": len(cases),
    }

    total_variants = sum(len(c.get("variants") or []) for c in cases)
    est_seconds = int(total_variants * (args.delay + 3))   # ~3s LLM latency per call
    print(f"Running {len(cases)} cases ({total_variants} variants) against {args.api_base}...")
    print(f"  delay={args.delay}s between requests; estimated runtime ~{est_seconds // 60}m{est_seconds % 60}s")
    results: list[CaseResult] = []
    for case in cases:
        results.append(run_case(case, args.api_base, cookie, args.db_path,
                                delay_s=args.delay))
        if args.delay > 0:
            time.sleep(args.delay)  # also pace BETWEEN cases

    _fmt_console(results)
    if args.json:
        _write_json(results, args.json, meta)
    if args.md:
        _write_markdown(results, args.md, meta)

    failing = sum(1 for r in results if not r.overall_pass)
    return 1 if failing else 0


def _fail(msg: str) -> int:
    print(f"\033[31mERROR:\033[0m {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
