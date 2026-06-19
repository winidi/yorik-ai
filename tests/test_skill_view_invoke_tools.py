"""Phase 2 — SkillViewTool + InvokeSkillTool basic contracts.

These two tools land in front of the LLM in Phase 3. Pin their shape
now so later prompt/registry refactors can't quietly change what the
model sees when it calls them.
"""
from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def stub_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Same isolation pattern as test_registry_index: hand-rolled skills
    in a tmp dir so we don't depend on the live backend/skills/ tree.
    Patches get_registry() to return our stub for the duration of the
    test so the tools see exactly these three skills."""
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
            "async def execute(ctx, **kwargs):\n"
            "    return {'echo': kwargs}\n",
            encoding="utf-8",
        )

    _make(
        "stub_read",
        textwrap.dedent("""
            name: stub_read
            description: Stub read-only skill for tests
            permissions: [admin, member]
            inputs:
              query:
                type: string
                required: true
            tags: [test, read]
        """),
        "Long body — `Key Concepts` and so on.",
    )
    _make(
        "stub_write",
        textwrap.dedent("""
            name: stub_write
            description: Stub mutation skill for tests
            permissions: [admin, member]
            tags: [test, write]
        """),
    )

    from backend.skills.registry import Registry, _load_one
    reg = Registry()
    for d in sorted(skills_dir.iterdir()):
        reg.register(_load_one(d, d / "skill.md", d / "skill.py"))

    monkeypatch.setattr("backend.skills.get_registry", lambda: reg)
    return reg


def _ctx() -> object:
    """Minimal stand-in for the Vanna ToolContext. Carries user_id."""
    m = MagicMock()
    m.user_id = 1
    m.metadata = {}
    m.user = MagicMock(role="admin")
    return m


class TestSkillViewTool:
    def test_returns_full_manifest_on_known_skill(self, stub_registry):
        from backend.ui_tools import SkillViewTool, SkillViewArgs
        tool = SkillViewTool()
        result = asyncio.run(
            tool.execute(_ctx(), SkillViewArgs(name="stub_read"))
        )
        assert result.success is True
        import json
        parsed = json.loads(result.result_for_llm)
        assert parsed["name"] == "stub_read"
        assert parsed["description"] == "Stub read-only skill for tests"
        # Body included so the LLM can read Key Concepts / Verification etc.
        assert "Key Concepts" in parsed["body"]
        # The inputs schema must round-trip — the LLM uses it to build args.
        assert parsed["inputs"]["query"]["required"] is True

    def test_unknown_skill_is_a_clean_error(self, stub_registry):
        from backend.ui_tools import SkillViewTool, SkillViewArgs
        tool = SkillViewTool()
        result = asyncio.run(
            tool.execute(_ctx(), SkillViewArgs(name="does_not_exist"))
        )
        assert result.success is False
        assert result.error == "unknown_skill"
        # The error message tells the LLM to rescan its skill index
        # rather than retrying with another guess.
        assert "skill index" in result.result_for_llm.lower()

    def test_no_side_effects(self, stub_registry):
        """skill_view must NOT call the skill's execute() — it only
        reads the manifest. Sanity-check by counting calls via the
        registry's invoke (which we don't expose here, but we can prove
        the skill's execute wasn't reached by checking the result text
        doesn't include the echo payload format)."""
        from backend.ui_tools import SkillViewTool, SkillViewArgs
        tool = SkillViewTool()
        result = asyncio.run(
            tool.execute(_ctx(), SkillViewArgs(name="stub_read"))
        )
        # 'echo' is the marker the stub's execute() would have emitted.
        # Its absence from the result_for_llm proves we never ran the skill.
        assert "echo" not in result.result_for_llm


class TestInvokeSkillTool:
    def test_dispatches_like_use_skill(self, stub_registry):
        """InvokeSkillTool is a subclass of UseSkillTool with a different
        name. Its execute() must produce the same result shape, so the
        LLM sees no behavioural difference once it switches."""
        from backend.ui_tools import InvokeSkillTool, UseSkillTool, UseSkillArgs

        invoke = InvokeSkillTool()
        use = UseSkillTool()

        args = UseSkillArgs(name="stub_read", args={"query": "hello"})

        r_invoke = asyncio.run(invoke.execute(_ctx(), args))
        r_use = asyncio.run(use.execute(_ctx(), args))

        assert r_invoke.success == r_use.success == True
        assert r_invoke.result_for_llm == r_use.result_for_llm

    def test_name_property_is_invoke_skill(self):
        from backend.ui_tools import InvokeSkillTool
        assert InvokeSkillTool().name == "invoke_skill"

    def test_description_references_skill_view(self):
        """The description must explicitly mention skill_view so the LLM
        learns the pair from the tool list alone. Phase 3's system
        prompt also reinforces this, but the tool description carries
        its own weight."""
        from backend.ui_tools import InvokeSkillTool
        desc = InvokeSkillTool().description
        assert "skill_view" in desc

    def test_unknown_skill_same_error_path_as_use_skill(self, stub_registry):
        from backend.ui_tools import InvokeSkillTool, UseSkillArgs
        tool = InvokeSkillTool()
        result = asyncio.run(
            tool.execute(_ctx(), UseSkillArgs(name="nope", args={}))
        )
        assert result.success is False
        assert "list_skills" in result.result_for_llm or "unknown" in result.error.lower()
