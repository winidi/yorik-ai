"""Phase 1 — registry.index() + registry.view() shape contracts.

These guard the LLM-visible skill catalog. The index goes into the
system prompt every turn; the view() output goes into the model's
context window when it calls the skill_view tool. Both shapes are
LLM-facing surfaces, so this test pins them as deliberate contracts
that future refactors must respect.
"""
from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def stub_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A registry with three hand-rolled skills. Real loader, fake skill
    dirs — keeps the test isolated from the live backend/skills/ tree
    so adding/removing real skills can't break index assertions."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    def _make(name: str, frontmatter: str, body: str = "") -> None:
        d = skills_dir / name
        d.mkdir()
        (d / "skill.md").write_text(
            f"---\n{frontmatter.strip()}\n---\n\n{body}",
            encoding="utf-8",
        )
        (d / "skill.py").write_text(
            "async def execute(ctx, **kwargs):\n    return {'ok': True}\n",
            encoding="utf-8",
        )

    _make(
        "z_lookup_thing",
        textwrap.dedent("""
            name: z_lookup_thing
            description: Find a thing
            permissions: [admin, member]
            tags: [lookup, read]
        """),
        "Body for z_lookup_thing.\n\n## Key Concepts\n- Things have ids.",
    )
    _make(
        "a_do_thing",
        textwrap.dedent("""
            name: a_do_thing
            description: Mutate a thing
            permissions: [admin]
            category: calendar
            tags: [calendar, write]
        """),
    )
    _make(
        "m_admin_only",
        textwrap.dedent("""
            name: m_admin_only
            description: Powerful op
            permissions: [admin]
            tags: [admin]
        """),
    )

    from backend.skills.registry import Registry, load_all

    monkeypatch.setattr(
        "backend.skills.registry._SKILLS_DIR", skills_dir,
        raising=False,
    )
    # _SKILLS_DIR may not exist as an attribute; load_all signature varies.
    # We'll call _load_one directly to bypass any module-level path constant.
    from backend.skills.registry import _load_one

    reg = Registry()
    for d in sorted(skills_dir.iterdir()):
        reg.register(_load_one(d, d / "skill.md", d / "skill.py"))
    return reg


class TestIndex:
    def test_index_returns_one_row_per_skill(self, stub_registry):
        rows = stub_registry.index()
        assert len(rows) == 3
        names = {r["name"] for r in rows}
        assert names == {"z_lookup_thing", "a_do_thing", "m_admin_only"}

    def test_index_row_shape_is_compact(self, stub_registry):
        rows = stub_registry.index()
        for r in rows:
            assert set(r.keys()) == {"name", "description", "category", "permissions"}
            assert isinstance(r["name"], str) and r["name"]
            assert isinstance(r["description"], str) and r["description"]
            assert isinstance(r["category"], str) and r["category"]
            assert isinstance(r["permissions"], list)

    def test_index_sorted_by_category_then_name(self, stub_registry):
        rows = stub_registry.index()
        ordering = [(r["category"], r["name"]) for r in rows]
        assert ordering == sorted(ordering)

    def test_index_filters_by_role(self, stub_registry):
        # member can see z_lookup_thing (admin+member), not the others.
        member_rows = stub_registry.index(role="member")
        names = {r["name"] for r in member_rows}
        assert names == {"z_lookup_thing"}

        # admin sees everything.
        admin_rows = stub_registry.index(role="admin")
        assert {r["name"] for r in admin_rows} == {
            "z_lookup_thing", "a_do_thing", "m_admin_only",
        }

    def test_index_role_none_keeps_all(self, stub_registry):
        # role=None means "I'm not asking about permissions" — include
        # everything so the /api/skills inspection endpoint can still
        # show admin-only skills to admins via that pathway.
        rows = stub_registry.index()
        assert len(rows) == 3

    def test_category_falls_back_to_first_tag(self, stub_registry):
        rows_by_name = {r["name"]: r for r in stub_registry.index()}
        # z_lookup_thing has no explicit category, tags=[lookup, read]
        assert rows_by_name["z_lookup_thing"]["category"] == "lookup"
        # a_do_thing has explicit category, wins
        assert rows_by_name["a_do_thing"]["category"] == "calendar"
        # m_admin_only has no category, tags=[admin]
        assert rows_by_name["m_admin_only"]["category"] == "admin"


class TestView:
    def test_view_returns_full_manifest_with_body(self, stub_registry):
        v = stub_registry.view("z_lookup_thing")
        assert v is not None
        assert v["name"] == "z_lookup_thing"
        assert v["description"] == "Find a thing"
        assert v["category"] == "lookup"
        assert v["permissions"] == ["admin", "member"]
        assert "Key Concepts" in v["body"]

    def test_view_unknown_returns_none(self, stub_registry):
        assert stub_registry.view("does_not_exist") is None

    def test_view_includes_body_unmodified(self, stub_registry):
        # body is the raw markdown after frontmatter; whitespace at edges
        # is stripped by _load_one but inner formatting must survive.
        v = stub_registry.view("z_lookup_thing")
        assert v["body"].strip().startswith("Body for z_lookup_thing.")
        assert "## Key Concepts" in v["body"]


class TestWhenNotToUse:
    def test_when_not_to_use_field_loads_and_surfaces_in_view(
        self, tmp_path, monkeypatch
    ):
        """Phase 8c: skill.md frontmatter may declare when_not_to_use
        as a structured peer of when_to_use. The field must round-trip
        through the loader, the Skill dataclass, and skill_view output."""
        skills_dir = tmp_path / "neg_skills"
        skills_dir.mkdir()
        d = skills_dir / "negz"
        d.mkdir()
        (d / "skill.md").write_text(
            textwrap.dedent("""
                ---
                name: negz
                description: Demo
                when_to_use: |
                  Use for X.
                when_not_to_use: |
                  Don't use for Y - call other_skill instead.
                permissions: [admin, member]
                ---
                body
            """).strip(),
            encoding="utf-8",
        )
        (d / "skill.py").write_text(
            "async def execute(ctx, **kwargs):\n    return {'ok': True}\n",
            encoding="utf-8",
        )

        from backend.skills.registry import _load_one, Registry
        skill = _load_one(d, d / "skill.md", d / "skill.py")
        assert skill.when_not_to_use.strip().startswith("Don't use for Y")

        reg = Registry()
        reg.register(skill)
        v = reg.view("negz")
        assert v["when_not_to_use"].strip().startswith("Don't use for Y")
        # Field present even when not declared (empty string default).
        assert "when_not_to_use" in v

    def test_when_not_to_use_defaults_to_empty_when_absent(self, stub_registry):
        """Skills that don't declare it get an empty string, not a KeyError."""
        v = stub_registry.view("z_lookup_thing")
        assert v["when_not_to_use"] == ""


class TestInvokeNameCollision:
    def test_invoke_accepts_name_kwarg_for_skill(self, stub_registry):
        # Regression: find_person legitimately takes a `name` input.
        # Before this fix, `invoke('find_person', name='X')` collided
        # with invoke's own `name` parameter, raising
        # "got multiple values for argument 'name'". Now `name` is
        # positional-only on invoke, so kwargs.name is passed through.
        result = asyncio.run(stub_registry.invoke(
            "z_lookup_thing", name="zu Hause"
        ))
        assert result == {"ok": True}
