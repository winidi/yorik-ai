"""Phase 9.1: ensure every skill has explicit role-permission intent.

The Phase 7.8 bug — check_tasks defaulted to admin-only because its
manifest didn't declare `permissions:` — was the second time we'd
hit that class of bug. The first was Phase 6 with the calendar
mutation skills. Both times a member legitimately needed the skill
and got "permission denied" without the manifest author realizing.

This test walks every loaded skill and asserts EITHER:
  - The manifest explicitly declares `permissions:` (any non-empty list)
  - OR the skill is in `EXPECTED_ADMIN_ONLY` — a known, intentional
    allowlist that this test maintainer reviewed.

Adding a new skill without thinking about permissions will now fail
in CI rather than silently shipping as admin-only.
"""
from __future__ import annotations


# Skills that are intentionally admin-only — usually because they
# touch financial data or because they're maintenance / debug paths
# the household admin alone should reach.
#
# If you add to this list, add a one-line comment justifying it.
EXPECTED_ADMIN_ONLY: set[str] = {
    "add_bill",        # financial: only the household's admin manages bills
    "check_bills",     # financial: paired with admin write access
    "delete_bill",     # financial
    "update_bill",     # financial
}


def _load_registry():
    """Load the real backend/skills/ tree (not a stub). The test runs
    against the actual shipped manifests so a misconfigured
    permissions line is caught."""
    from backend.skills.registry import load_all
    return load_all()


def test_every_skill_has_explicit_or_intentionally_default_permissions():
    """Each skill must declare `permissions:` in its skill.md frontmatter,
    OR be in EXPECTED_ADMIN_ONLY. The implicit-admin default is too
    easy to miss; this test forces the choice."""
    reg = _load_registry()
    bad: list[str] = []
    for skill in reg._skills.values():
        # The dataclass default is ["admin"]; we can't distinguish
        # "explicitly [admin]" from "missing field" at runtime via
        # `permissions` alone. Instead, read the raw frontmatter from
        # the manifest file: if the file lacks a 'permissions:' line,
        # it relied on the default.
        if not skill.manifest_path or not skill.manifest_path.exists():
            continue
        raw = skill.manifest_path.read_text(encoding="utf-8")
        # Only look inside the frontmatter (between the first two ---).
        parts = raw.split("---", 2)
        if len(parts) < 3:
            continue
        frontmatter = parts[1]
        declares_permissions = any(
            line.lstrip().startswith("permissions:")
            for line in frontmatter.splitlines()
        )
        if not declares_permissions and skill.name not in EXPECTED_ADMIN_ONLY:
            bad.append(skill.name)

    if bad:
        raise AssertionError(
            "Skills with implicit admin-only default (missing "
            "`permissions:` in skill.md, and not in "
            "EXPECTED_ADMIN_ONLY allowlist):\n  - "
            + "\n  - ".join(sorted(bad))
            + "\n\nFix: declare `permissions: [admin, member, ...]` "
            "in the manifest, or add the skill to EXPECTED_ADMIN_ONLY "
            "with a one-line justification."
        )


def test_check_tasks_open_to_all_read_roles():
    """Regression guard for Phase 7.8 — members must be able to read
    their own tasks via check_tasks."""
    reg = _load_registry()
    skill = reg.get("check_tasks")
    assert skill is not None
    assert "member" in skill.permissions
    assert "child" in skill.permissions


def test_compose_draft_open_to_members():
    """Regression guard for Phase 9.1 — members write letters too."""
    reg = _load_registry()
    skill = reg.get("compose_draft")
    assert skill is not None
    assert "member" in skill.permissions


def test_undo_last_action_open_to_members():
    """Regression guard for Phase 9.1 — the skill body explicitly says
    'each user can only undo their own actions', so it must be reachable
    by members (the gate is per-user inside the skill, not per-role)."""
    reg = _load_registry()
    skill = reg.get("undo_last_action")
    assert skill is not None
    assert "member" in skill.permissions
