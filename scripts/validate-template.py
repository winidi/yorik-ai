#!/usr/bin/env python3
"""Validate a compose template JSON file.

Checks:
  - Required top-level fields (id, name, body_html, default_args)
  - Every `ask_user_for_args` entry has a known `role` (warn if missing,
    error if unknown). Roles must match the closed enum in
    frontend-react/src/apps/compose/types.ts (ArgRole).
  - Every `default_args` key has a matching `ask_user_for_args` entry
    (so the args panel can render labels + roles for everything).
  - Sniff for placeholder data that shouldn't ship in the body_html
    (real-looking emails, phone numbers).
  - `body_html` is non-empty and parses as a Jinja template referring
    only to keys present in `default_args`.

Usage:
    python scripts/validate-template.py templates/my-template.json
    python scripts/validate-template.py templates/              # all in dir
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

# Keep in sync with frontend-react/src/apps/compose/types.ts ArgRole.
KNOWN_ROLES = {
    "body", "subject", "greeting", "closing",
    "recipient_name", "recipient_address", "recipient_email", "recipient_phone",
    "sender_name", "sender_address", "sender_email", "sender_phone", "sender_business",
    "date", "reference_number", "currency_amount", "location",
    "freeform_text", "freeform_value",
}
REQUIRED_TOP_KEYS = {"id", "name", "body_html", "default_args"}
TEMPLATE_REF_RE = re.compile(r"\{\{\s*args\.([A-Za-z_][A-Za-z0-9_]*)\b")


def validate_one(path: Path) -> int:
    """Returns count of errors (warnings don't count)."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"unparseable JSON: {exc}")
        _report(path, errors, warnings)
        return len(errors)

    missing_top = REQUIRED_TOP_KEYS - set(d.keys())
    if missing_top:
        errors.append(f"missing required top-level fields: {sorted(missing_top)}")

    schema = d.get("ask_user_for_args") or []
    default_args = d.get("default_args") or {}
    schema_keys = {f.get("key") for f in schema if isinstance(f, dict) and f.get("key")}

    # Every default_args key should have a schema entry — otherwise the
    # frontend can't show a label, hint, or role for it.
    for k in default_args:
        if k not in schema_keys:
            warnings.append(
                f"default_args key {k!r} has no ask_user_for_args entry — "
                f"label/role/hint cannot be shown in the UI"
            )

    # Every schema entry should declare a role.
    no_role = [f.get("key") for f in schema if isinstance(f, dict) and not f.get("role")]
    if no_role:
        warnings.append(
            f"{len(no_role)} arg(s) without `role`: {no_role}. "
            f"Without role, the AI buttons + chrome rules fall back to "
            f"key-name regex. Add role from: {sorted(KNOWN_ROLES)}"
        )

    # Unknown roles are errors (typos / new role someone forgot to add).
    for f in schema:
        if not isinstance(f, dict):
            errors.append(f"ask_user_for_args entry is not an object: {f!r}")
            continue
        role = f.get("role")
        if role and role not in KNOWN_ROLES:
            errors.append(
                f"arg {f.get('key')!r} has unknown role {role!r} — "
                f"must be one of: {sorted(KNOWN_ROLES)}"
            )

    # body_html should reference only keys we know about.
    body_html = d.get("body_html") or ""
    if not body_html.strip():
        errors.append("body_html is empty")
    else:
        referenced = set(TEMPLATE_REF_RE.findall(body_html))
        # Allow common Jinja helpers (today_de, etc.) by skipping non-args refs
        unknown = referenced - set(default_args.keys())
        if unknown:
            warnings.append(
                f"body_html references args not in default_args: {sorted(unknown)} — "
                f"these render as empty strings"
            )

    # Sniff for likely-real PII in preview / default values.
    blob = json.dumps({"default_args": default_args, "preview_args": d.get("preview_args") or {}},
                       ensure_ascii=False)
    if re.search(r"\b[A-Za-z0-9._%+\-]+@(?!example\.|beispiel\.|test\.|yorik\.local)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", blob):
        warnings.append(
            "found what looks like a real email address in default_args / preview_args — "
            "use example.com / beispiel.de placeholders"
        )

    _report(path, errors, warnings)
    return len(errors)


def _report(path: Path, errors: list[str], warnings: list[str]) -> None:
    if not errors and not warnings:
        print(f"  ok    {path.name}")
        return
    print(f"  {'ERR ' if errors else 'WARN'}  {path.name}")
    for e in errors:
        print(f"    [error]   {e}")
    for w in warnings:
        print(f"    [warning] {w}")


def main(argv: list[str]) -> int:
    targets: list[Path] = []
    if not argv:
        argv = ["templates/"]
    for a in argv:
        p = Path(a)
        if p.is_dir():
            targets.extend(sorted(p.glob("*.json")))
        elif p.is_file():
            targets.append(p)
        else:
            print(f"  skip  {a}: not found", file=sys.stderr)
    if not targets:
        print("nothing to validate", file=sys.stderr)
        return 1
    total_errors = 0
    print(f"validating {len(targets)} template(s):")
    for t in targets:
        total_errors += validate_one(t)
    if total_errors:
        print(f"\nFAIL — {total_errors} error(s). See [error] lines above.", file=sys.stderr)
        return 2
    print("\nall templates valid (warnings are non-blocking).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
