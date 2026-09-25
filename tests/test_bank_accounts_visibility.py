"""Finance app: a private bank account stays private, a shared one is
visible to everyone in the household's Finance space — no admin
exception, same rule as everything else in Yorik.

See docs/plans/2026-09-25-finanzen.md."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import login_client


@pytest.fixture
def household(fresh_app):
    from backend import spaces as S
    from backend.database import get_conn

    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate")):
        S.ensure_personal_space(uid, name)

    with get_conn() as conn:
        finance_space = conn.execute("SELECT id FROM spaces WHERE slug = 'finance'").fetchone()
        finance_space_id = int(finance_space["id"])
        # Beate needs an explicit membership to see the shared space —
        # only the workspace owner (Dirk) is auto-added on setup.
        conn.execute(
            "INSERT INTO space_members (space_id, user_id, level) VALUES (?, ?, 'read')",
            (finance_space_id, beate),
        )

        def _acct(owner, name, space_id):
            cur = conn.execute(
                "INSERT INTO bank_accounts "
                "(owner_user_id, space_id, display_name, bank_url, blz, login_name, credential_key) "
                "VALUES (?, ?, ?, 'https://example.invalid', '00000000', 'x', 'unused') RETURNING id",
                (owner, space_id, name),
            )
            return int(cur.fetchone()["id"])

        dirk_private = _acct(dirk, "Dirks Sparkasse (privat)", None)
        beate_private = _acct(beate, "Beates ING (privat)", None)
        shared = _acct(dirk, "Gemeinsames Konto", finance_space_id)
        conn.commit()

    return {"dirk": dirk, "beate": beate, "dirk_private": dirk_private,
            "beate_private": beate_private, "shared": shared}


def _accounts_seen(uid: str, role: str) -> dict[int, str]:
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.list_bank_accounts.skill import execute
    out = asyncio.run(execute(ctx=SkillContext(Registry(), role=role, user_id=uid)))
    return {a["id"]: a["display_name"] for a in out["accounts"]}


def test_owner_sees_own_and_shared_not_the_other_persons_private(household):
    seen = _accounts_seen(household["dirk"], "platform_admin")
    assert household["dirk_private"] in seen
    assert household["shared"] in seen
    assert household["beate_private"] not in seen


def test_other_household_member_sees_own_and_shared_not_dirks_private(household):
    seen = _accounts_seen(household["beate"], "member")
    assert household["beate_private"] in seen
    assert household["shared"] in seen
    assert household["dirk_private"] not in seen


def test_admin_gets_no_exception_for_another_persons_private_account(household):
    """Same rule as calendars/recordings: being platform_admin does not
    open a door into someone else's private space."""
    seen = _accounts_seen(household["dirk"], "platform_admin")
    assert household["beate_private"] not in seen
