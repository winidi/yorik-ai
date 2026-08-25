"""Refining a compose draft keeps the draft's template.

A refine call from the model usually carries only `existing_draft_id`
plus the changed args. compose_draft used to resolve the template
before it read the draft row, so the generic-letter fallback won and
the refine was judged (and rejected) against the wrong template's
required fields.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import seed_user


def _mk_ctx(*, role: str, user_id: str):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=user_id)


@pytest.fixture
def admin(fresh_app):
    from backend import spaces as _sp
    uid = seed_user(name="Composer", role="admin", email="composer@example.com", language="de")
    _sp.ensure_workspace_exists(uid, "Composer")
    _sp.ensure_personal_space(uid, "Composer")
    return uid


def _draft_row(draft_id: int):
    from backend.database import get_conn
    with get_conn() as conn:
        return dict(conn.execute(
            "SELECT id, template_id, kind, recipient, subject, args_json FROM compose_drafts WHERE id = ?",
            (draft_id,),
        ).fetchone())


def test_refine_keeps_the_drafts_template(admin):
    from backend.skills.compose_draft.skill import execute
    ctx = _mk_ctx(role="admin", user_id=admin)

    created = asyncio.run(execute(
        ctx=ctx, template_id="generic-email-en", kind="email",
        recipient="Anna Example", subject="Quick question",
        args={"recipient_name": "Anna Example", "recipient_email": "anna@example.com",
              "body_text": "Are we still on for Friday?"},
    ))
    draft_id = created.get("draft_id")
    assert draft_id, created
    assert _draft_row(draft_id)["template_id"] == "generic-email-en"

    # The refine the model actually sends: draft id + the one changed key.
    refined = asyncio.run(execute(
        ctx=ctx, existing_draft_id=draft_id,
        args={"body_text": "Are we still on for Friday at 7?"},
    ))
    assert refined.get("ok") is not False, refined
    assert "REJECTED" not in (refined.get("_llm_hint") or "")
    row = _draft_row(draft_id)
    assert row["template_id"] == "generic-email-en", "refine must not swap the template"
    assert "Friday at 7" in (row["args_json"] or "")
    assert refined.get("draft_id") == draft_id, "refine must update, not create a second draft"
