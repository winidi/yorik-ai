#!/usr/bin/env python3
"""Yorik skill + chat smoke harness.

Goal: catch the "no such column" / "wrong kwarg" class of bug WITHOUT
running through the LLM. Hits every skill via the public
POST /api/skills/{name}/invoke endpoint with curated synthetic args,
classifies each result, then runs a small chat probe to verify the
agent loop end-to-end.

Usage:
    bash scripts/restart-uvicorn.sh --no-reload
    python scripts/smoke-skills.py                 # smoke all skills
    python scripts/smoke-skills.py --skill add_task   # just one
    python scripts/smoke-skills.py --no-chat       # skip chat probes
    BASE=http://192.168.122.49:8000 python scripts/smoke-skills.py

Exit code 0 = green, 1 = at least one unexpected failure.

What it does NOT cover:
- Skills that depend on external state (live email server, paired
  WhatsApp, configured connector creds) — those are SKIPPED with a
  reason rather than failed.
- Dynamic UPDATE SQL where the SET clause is built from the args
  passed in — only the specific fields we exercise here get checked.
- Multi-step flows (compose draft → render PDF → send) — those need
  their own integration tests.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional
from urllib.parse import urljoin

import requests

BASE = os.environ.get("BASE", "http://localhost:8000")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@yorik.local")
ADMIN_PASS  = os.environ.get("ADMIN_PASS",  "admin1234")


# ─── Per-skill test recipes ─────────────────────────────────────────
#
# Each entry: skill_name -> dict of behaviour.
#
#   args:    dict — kwargs to invoke with. Can be a callable that takes
#            `ctx` (a SmokeCtx with helper accessors) and returns dict.
#   skip:    str — if present, the skill is skipped with this reason.
#   expect:  "ok" (default) | "any" (don't classify) | "error_substr"
#            tolerate the error if it contains this string (for skills
#            where empty-state IS the expected response).
#   needs:   list of context keys (e.g. ["test_event_id"]) that have to
#            be set by an earlier skill in the run. Skipped otherwise.

def _safe_event_args(ctx):
    return {
        "title":     "[smoke] test event",
        "starts_at": "2027-01-15T10:00:00",
        "ends_at":   "2027-01-15T11:00:00",
    }

def _update_event_args(ctx):
    eid = ctx.state.get("test_event_id")
    return {"event_id": eid, "title": "[smoke] test event (renamed)"}

def _delete_event_args(ctx):
    return {"event_id": ctx.state.get("test_event_id")}

def _block_travel_args(ctx):
    return {"event_id": ctx.state.get("test_event_id"), "minutes": 30}

def _add_task_args(ctx):
    return {"title": "[smoke] test task"}

def _update_task_args(ctx):
    return {"task_id": ctx.state.get("test_task_id"), "done": True}

def _delete_task_args(ctx):
    return {"task_id": ctx.state.get("test_task_id")}

def _add_contact_args(ctx):
    return {
        "display_name": "[smoke] Test Contact",
        "kind":         "person",
    }

def _update_contact_args(ctx):
    return {"contact_id": ctx.state.get("test_contact_id"),
            "relation":   "friend"}

def _delete_contact_args(ctx):
    return {"contact_id": ctx.state.get("test_contact_id")}

def _add_contact_channel_args(ctx):
    return {"contact_id": ctx.state.get("test_contact_id"),
            "kind": "email", "value": "smoke@example.com"}

def _add_contact_address_args(ctx):
    return {"contact_id": ctx.state.get("test_contact_id"),
            "line1": "Smokestr. 1", "postcode": "12345", "city": "Testdorf"}

def _share_contact_args(ctx):
    # Sharing with self is harmless and exercises the code path.
    return {"contact_id": ctx.state.get("test_contact_id"),
            "with_user_id": ctx.user_id, "can_edit": False}

def _unshare_contact_args(ctx):
    return {"contact_id": ctx.state.get("test_contact_id"),
            "with_user_id": ctx.user_id}

def _promote_pending_args(ctx):
    # Promoting an active contact is a no-op; exercises the SQL path
    return {"contact_id": ctx.state.get("test_contact_id")}

def _mark_spam_args(ctx):
    # Just exercise the call shape; the skill toggles spam state.
    return {"contact_id": ctx.state.get("test_contact_id")}


RECIPES: dict[str, dict[str, Any]] = {
    # ─── reads ─────────────────────────────────────────────────────
    "check_calendar":          {"args": {}},
    "check_tasks":             {"args": {}, "expect": "any"},
    "check_bills":             {"args": {}, "expect": "any"},
    "list_compose_templates":  {"args": {}},
    "list_contacts_for_picking": {"args": {}},
    "find_user":               {"args": {"query": "admin"}, "expect": "any"},
    "find_person":             {"args": {"query": "admin"}},
    "find_contact":            {"args": {"query": "admin"}, "expect": "any"},
    "find_photo":              {"args": {"query": "family"}, "expect": "any"},
    "find_document":           {"args": {"query": "test"}, "expect": "any"},
    "search_documents":        {"args": {"query": "test"}, "expect": "any"},
    "find_known_provider":     {"args": {"category": "dentist"}, "expect": "any"},
    "find_provider_nearby":    {"args": {"poi": "dentist"}, "skip": "needs maps connector + location context"},
    "find_recipient_address_from_documents": {"args": {"recipient_name": "test"}, "expect": "any"},
    "calculate_travel_time":   {"args": {"to": "Hauptstr. 1, Berlin"}, "skip": "needs maps connector + user home address"},
    "compute_group_price":     {"args": {"prices_url": "https://example.com", "people": [{"name":"smoke","age":30}]}, "skip": "needs web access"},
    "extract_price_table":     {"args": {"page_text": "Adult 5 EUR\nKid 2 EUR"}},
    "navigate_to":             {"args": {"app": "home"}},
    "yorik_help":              {"args": {"topic": "tailscale"}},
    "universal_search":        {"args": {"query": "test"}, "expect": "any"},
    "propose_meeting_times":   {"skip": "needs an inbound email/message_id reference"},
    "propose_inline_photo":    {"skip": "needs an event with location + Immich photos"},
    "save_venue":              {"skip": "needs a fresh POI not in contacts"},

    # ─── writes (chained: setup → use → cleanup) ───────────────────
    "add_calendar_event":      {"args": _safe_event_args, "captures": "test_event_id", "from_key": "event_id"},
    "update_calendar_event":   {"args": _update_event_args, "needs": ["test_event_id"]},
    "block_travel_time":       {"args": _block_travel_args, "needs": ["test_event_id"]},
    "delete_calendar_event":   {"args": _delete_event_args, "needs": ["test_event_id"]},

    "add_task":                {"args": _add_task_args, "captures": "test_task_id", "from_key": "id"},
    "update_task":             {"args": _update_task_args, "needs": ["test_task_id"]},
    "delete_task":             {"args": _delete_task_args, "needs": ["test_task_id"]},

    "add_contact":             {"args": _add_contact_args, "captures": "test_contact_id", "from_key": "contact_id"},
    "update_contact":          {"args": _update_contact_args, "needs": ["test_contact_id"]},
    "add_contact_channel":     {"args": _add_contact_channel_args, "needs": ["test_contact_id"]},
    "add_contact_address":     {"args": _add_contact_address_args, "needs": ["test_contact_id"]},
    "share_contact":           {"args": _share_contact_args, "needs": ["test_contact_id"]},
    "unshare_contact":         {"args": _unshare_contact_args, "needs": ["test_contact_id"]},
    "promote_pending_contact": {"args": _promote_pending_args, "needs": ["test_contact_id"]},
    "mark_contact_spam":       {"args": _mark_spam_args, "needs": ["test_contact_id"]},
    "delete_contact":          {"args": _delete_contact_args, "needs": ["test_contact_id"]},

    # ─── compose / write flows (skipped; need template setup) ──────
    "compose_draft":              {"skip": "compose flow needs template + recipient setup"},
    "compose_check_recipient":    {"skip": "needs in-flight compose state"},
    "compose_check_template_args":{"skip": "needs in-flight compose state"},
    "compose_extract_args":       {"skip": "needs a paste source"},
    "delete_compose_draft":       {"skip": "needs draft id"},

    # ─── bills (admin-only, sandboxed creation is heavy) ───────────
    "add_bill":                {"skip": "bills creation has side effects (numbering series)"},
    "update_bill":             {"skip": "needs existing bill id"},
    "delete_bill":             {"skip": "needs existing bill id"},

    # ─── communications (need real backends) ───────────────────────
    "email_draft":             {"skip": "needs IMAP account configured"},
    "email_briefing":          {"skip": "needs IMAP account configured"},
    "whatsapp_draft":          {"skip": "needs paired WhatsApp"},
    "whatsapp_briefing":       {"skip": "needs paired WhatsApp"},
    "set_document_visibility": {"skip": "needs paperless doc id"},

    # ─── housekeeping / misc ───────────────────────────────────────
    "undo_last_action":        {"skip": "needs a pending_action staged just before"},
    "add_calendar":            {"args": {"name": "[smoke] cal"}, "skip": "creates persistent cal; manual cleanup"},
    "trigger_connector":       {"skip": "needs connector configured"},
}


# ─── Chat probes ────────────────────────────────────────────────────

CHAT_PROBES = [
    # (user_prompt, must_contain_one_of [in response])
    ("welche aufgaben habe ich offen?",          ["aufgabe", "offen", "keine", "0 "]),
    ("welche termine habe ich diese woche?",     ["termin", "kalender", "woche", "keine", "0 "]),
    ("zeig mir den kalender",                    ["kalender"]),
    ("trag einen termin für morgen um 16 uhr für 30 minuten 'smoke probe' ein",
                                                 ["smoke", "morgen", "16:00", "eingetragen"]),
    ("welche kontakte habe ich?",                ["kontakt"]),
]


# ─── Harness plumbing ───────────────────────────────────────────────


class SmokeCtx:
    def __init__(self, session: requests.Session, user_id: int):
        self.session = session
        self.user_id = user_id
        self.state: dict[str, Any] = {}

    def invoke(self, skill: str, args: dict) -> tuple[int, Any]:
        # /api/skills/{name}/invoke treats the JSON body AS the args
        # dict (it's the single declared `args: dict = None` param on
        # the FastAPI handler). Don't wrap.
        r = self.session.post(
            urljoin(BASE, f"/api/skills/{skill}/invoke"),
            json=args,
            timeout=30,
        )
        try: body = r.json()
        except Exception: body = {"text": r.text[:300]}
        return r.status_code, body


def login() -> tuple[requests.Session, int]:
    s = requests.Session()
    r = s.post(urljoin(BASE, "/api/auth/login"),
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=10)
    r.raise_for_status()
    return s, int(r.json()["user"]["id"])


def list_skills(session: requests.Session) -> list[str]:
    r = session.get(urljoin(BASE, "/api/skills"), timeout=10)
    r.raise_for_status()
    body = r.json()
    rows = body if isinstance(body, list) else body.get("skills", [])
    return [row["name"] if isinstance(row, dict) else row for row in rows]


def run_skill_smoke(only: Optional[str] = None) -> tuple[int, int, int]:
    """Returns (pass, fail, skip)."""
    session, user_id = login()
    ctx = SmokeCtx(session, user_id)
    available = set(list_skills(session))
    target = [only] if only else sorted(available)

    n_pass = n_fail = n_skip = 0
    print(f"\n=== skill smoke ({len(target)} candidates against {BASE}) ===\n")
    # Process in a fixed order so chained setup→use→cleanup runs in the
    # right sequence — RECIPES dict insertion order does that.
    ordered = [s for s in RECIPES if s in target] + [
        s for s in target if s not in RECIPES
    ]
    for name in ordered:
        recipe = RECIPES.get(name, {"args": {}, "expect": "any"})

        # Not in registry?
        if name not in available:
            print(f"  ?  {name:32} not registered on this install")
            n_skip += 1
            continue

        # Explicit skip
        if "skip" in recipe:
            print(f"  -  {name:32} skip: {recipe['skip']}")
            n_skip += 1
            continue

        # Missing prerequisite from an earlier skill in the chain
        if recipe.get("needs"):
            missing = [k for k in recipe["needs"] if k not in ctx.state]
            if missing:
                print(f"  -  {name:32} skip: missing setup {missing}")
                n_skip += 1
                continue

        args = recipe["args"]
        if callable(args):
            args = args(ctx)

        status, body = ctx.invoke(name, args)

        # Capture an id from the result for chained skills
        if "captures" in recipe and status < 300:
            key = recipe["captures"]
            from_key = recipe.get("from_key", "id")
            val = None
            if isinstance(body, dict):
                val = body.get(from_key)
                # add_calendar_event returns nested 'event': {id: ...}
                if val is None and "event" in body and isinstance(body["event"], dict):
                    val = body["event"].get("id")
            if val is not None:
                ctx.state[key] = val

        if 200 <= status < 300:
            print(f"  ✓  {name:32} → {status}")
            n_pass += 1
        else:
            # Some skills return 400 with a "no results" message that's
            # totally fine for a smoke run (expect=any).
            tolerated = recipe.get("expect") in ("any",) or (
                isinstance(recipe.get("expect"), str)
                and recipe["expect"] in json.dumps(body)
            )
            if tolerated and status in (400, 404):
                print(f"  ·  {name:32} → {status} (tolerated: {str(body)[:80]})")
                n_pass += 1
            else:
                detail = body.get("detail") if isinstance(body, dict) else body
                print(f"  ✗  {name:32} → {status} {str(detail)[:140]}")
                n_fail += 1

    print(f"\n  pass={n_pass}  fail={n_fail}  skip={n_skip}")
    return n_pass, n_fail, n_skip


def run_chat_smoke() -> tuple[int, int]:
    """Returns (pass, fail)."""
    session, _ = login()
    n_pass = n_fail = 0
    print(f"\n=== chat smoke ({len(CHAT_PROBES)} prompts) ===\n")
    for prompt, must_contain in CHAT_PROBES:
        r = session.post(
            urljoin(BASE, "/api/ask"),
            json={"message": prompt},
            timeout=90,
        )
        if r.status_code != 200:
            print(f"  ✗  HTTP {r.status_code}  prompt={prompt!r}")
            n_fail += 1
            continue
        body = r.json()
        resp = (body.get("response") or body.get("reply") or "").lower()
        # Catch the obvious failure modes: empty response, the error
        # banner we surface on tool failures.
        if not resp.strip():
            print(f"  ✗  empty response  prompt={prompt!r}")
            n_fail += 1
            continue
        if any(s in resp for s in ("⛔", "tool failed", "no such column", "no such table")):
            print(f"  ✗  error banner in response  prompt={prompt!r}\n     resp={resp[:200]}")
            n_fail += 1
            continue
        # Soft assertion: at least one of must_contain should appear
        if must_contain and not any(s.lower() in resp for s in must_contain):
            print(f"  ?  weak match  prompt={prompt!r}\n     resp={resp[:200]}\n     expected one of {must_contain}")
            n_pass += 1  # still pass; LLM phrasing varies
        else:
            print(f"  ✓  {prompt[:50]!r:55} → {resp[:90]}")
            n_pass += 1
    print(f"\n  pass={n_pass}  fail={n_fail}")
    return n_pass, n_fail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill", help="run one named skill only")
    parser.add_argument("--no-chat", action="store_true", help="skip chat probes")
    parser.add_argument("--no-skills", action="store_true", help="skip skill probes")
    args = parser.parse_args()

    total_fail = 0
    if not args.no_skills:
        _, fail, _ = run_skill_smoke(args.skill)
        total_fail += fail

    if not args.no_chat and not args.skill:
        _, fail = run_chat_smoke()
        total_fail += fail

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
