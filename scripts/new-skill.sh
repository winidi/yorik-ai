#!/usr/bin/env bash
# Scaffold a new Yorik skill.
#
# Usage:  bash scripts/new-skill.sh <skill_name>
# Example: bash scripts/new-skill.sh weather_outlook
#
# Creates backend/skills/<skill_name>/ with skill.md + skill.py stubs
# you can edit immediately. After running, restart the backend and the
# LLM will pick up the new skill via list_skills.
#
# See docs/SKILLS.md for the full guide.

set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $# -lt 1 ]]; then
  echo "Usage: bash scripts/new-skill.sh <skill_name>" >&2
  echo "Example: bash scripts/new-skill.sh weather_outlook" >&2
  exit 1
fi

NAME="$1"

# Validate: lowercase letters, digits, underscores. No leading digit or _.
if ! [[ "$NAME" =~ ^[a-z][a-z0-9_]*$ ]]; then
  echo "✗ Skill name must be lowercase letters/digits/underscores, start with a letter." >&2
  echo "  Got: '$NAME'" >&2
  echo "  Examples: weather_outlook, dice_roll, send_to_slack" >&2
  exit 1
fi

DIR="backend/skills/$NAME"

if [[ -d "$DIR" ]]; then
  echo "✗ Skill already exists at $DIR — pick a different name or delete the existing one." >&2
  exit 1
fi

mkdir -p "$DIR"

# Friendly title for the docs (snake_case → "Snake Case")
TITLE=$(echo "$NAME" | tr '_' ' ' | awk '{for(i=1;i<=NF;i++)$i=toupper(substr($i,1,1)) tolower(substr($i,2))}1')

cat > "$DIR/skill.md" <<EOF
---
name: $NAME
description: TODO — one sentence: what does this skill do?
when_to_use: |
  - TODO: describe trigger phrases the user might say
  - "phrase 1"
  - "phrase 2"
  - When NOT to use: list cases where another skill is better
inputs:
  example_arg:
    type: string
    required: true
    description: TODO — what is this argument for?
outputs:
  result:
    type: string
    description: TODO — what comes back?
permissions: ["admin", "member"]
side_effects: none
cost: free
tags: [TODO]
---

# $TITLE

TODO — describe the skill in detail. What it does, what it doesn't do,
any quirks future maintainers should know. The LLM doesn't read past
the frontmatter, so this section is for humans.

Delete this paragraph when you're done.
EOF

cat > "$DIR/skill.py" <<EOF
"""$NAME skill — TODO one-line description."""
from __future__ import annotations
from typing import Any


async def execute(ctx, example_arg: str) -> dict[str, Any]:
    """Replace the signature + body with your real implementation.

    Conventions:
    - Validate inputs aggressively; the LLM is not your friend.
    - For mutations, follow the apply-then-rollback pattern — see
      docs/SKILLS.md and backend/skills/add_calendar_event/skill.py.
    - Return a flat-ish dict. Keep keys short. Avoid huge blobs.
    """
    if not example_arg or not example_arg.strip():
        raise ValueError("example_arg is required")

    return {
        "result": f"TODO: implement $NAME — you passed {example_arg!r}",
    }
EOF

echo "✓ Scaffolded skill at $DIR"
echo ""
echo "Next steps:"
echo "  1. Edit $DIR/skill.md — fill in the 'when_to_use' section (this is what makes the LLM pick your skill)"
echo "  2. Edit $DIR/skill.py — implement execute()"
echo "  3. Restart the backend so the registry picks up the new skill"
echo "  4. Test in chat — say one of your 'when_to_use' trigger phrases"
echo ""
echo "Full guide: docs/SKILLS.md"
