#!/usr/bin/env python3
"""Yorik skill-manifest linter — Hermes-style discipline.

Rules enforced (Phase 1 of the Hermes-style migration):

  1. description ≤100 chars                  ── stays compact in the always-on skill index
  2. description is exactly one sentence     ── single, scan-friendly trigger line
  3. description is non-empty and ≥20 chars  ── too terse hides intent from list_skills
  4. tags is a non-empty list                ── list_skills(query) needs keywords to hit
  5. required frontmatter fields present     ── name, description, when_to_use, permissions

Run from the yorik-ai repo root:

  python3 scripts/lint_skills.py

Exit 0 on clean, 1 on any violation. Intended for pre-commit + CI.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ROOT = Path("backend/skills")

DESCRIPTION_MAX_CHARS = 100
DESCRIPTION_MIN_CHARS = 20
REQUIRED_FIELDS = ("name", "description", "when_to_use", "permissions")

# Tokens that follow a period without ending a sentence (so we don't false-
# positive on abbreviations / decimals).
_NON_SENTENCE_TAIL = re.compile(r"\.\s+(?=[A-ZÄÖÜ])")


def _parse_frontmatter(text: str) -> dict[str, Any] | None:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return meta if isinstance(meta, dict) else None


def lint_skill(path: Path) -> list[str]:
    """Return a list of human-readable violation messages, or [] if clean."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"could not read: {exc}"]
    meta = _parse_frontmatter(text)
    if meta is None:
        return ["frontmatter missing or unparseable"]
    errors: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in meta or meta[field] in (None, "", []):
            errors.append(f"required field missing: {field}")

    desc = (meta.get("description") or "").strip()
    if desc:
        if len(desc) > DESCRIPTION_MAX_CHARS:
            errors.append(
                f"description {len(desc)} chars > {DESCRIPTION_MAX_CHARS} "
                f"(Hermes-style: one short sentence)"
            )
        if len(desc) < DESCRIPTION_MIN_CHARS:
            errors.append(
                f"description {len(desc)} chars < {DESCRIPTION_MIN_CHARS} (too terse)"
            )
        sentences = _NON_SENTENCE_TAIL.split(desc)
        if len(sentences) > 1:
            errors.append(
                f"description has {len(sentences)} sentences "
                f"(Hermes-style: one)"
            )

    tags = meta.get("tags")
    if not isinstance(tags, list) or not tags:
        errors.append("tags missing or empty (list_skills can't find this skill)")
    elif any(not isinstance(t, str) or not t.strip() for t in tags):
        errors.append("tags contains empty / non-string entries")

    return errors


def main() -> int:
    root = DEFAULT_ROOT
    if len(sys.argv) > 1:
        root = Path(sys.argv[1])
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2

    skills: list[tuple[str, Path]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("__"):
            continue
        md = child / "skill.md"
        if md.exists():
            skills.append((child.name, md))

    total = len(skills)
    failed = 0
    for name, path in skills:
        errs = lint_skill(path)
        if errs:
            failed += 1
            print(f"FAIL  {name}")
            for e in errs:
                print(f"        {e}")

    if failed == 0:
        print(f"\n  {total} skills checked, all clean.")
        return 0
    print(f"\n  {failed}/{total} skills failed linting.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
