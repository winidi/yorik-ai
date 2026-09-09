"""Admins run the box; they do not get a quiet look into other members' private spaces."""

from __future__ import annotations

from tests.conftest import seed_user


def test_admins_never_see_another_members_personal_space(fresh_app):
    from backend import spaces as S
    admin = seed_user(name="Admin", role="platform_admin", email="a@example.local")
    wadmin = seed_user(name="WAdmin", role="admin", email="w@example.local")
    member = seed_user(name="Beate", role="member", email="b@example.local")
    S.ensure_workspace_exists(admin, "Admin")
    for uid, name in ((admin, "Admin"), (wadmin, "WAdmin"), (member, "Beate")):
        S.ensure_personal_space(uid, name)
    from backend.database import get_conn
    with get_conn() as conn:
        rows = {r["owner_user_id"]: int(r["id"]) for r in conn.execute(
            "SELECT id, owner_user_id FROM spaces WHERE kind = 'personal'").fetchall()}
        shared = [int(r["id"]) for r in conn.execute("SELECT id FROM spaces WHERE kind <> 'personal'").fetchall()]
    beate_personal = rows[member]

    for uid, role in ((admin, "platform_admin"), (wadmin, "admin")):
        visible = set(S.user_visible_space_ids(uid, role))
        assert beate_personal not in visible
        assert rows[uid] in visible
        assert set(shared) <= visible or role == "admin"
        assert S.user_space_level(uid, beate_personal, role) is None
    # explicit tooling can still enumerate everything
    assert beate_personal in S.user_visible_space_ids(admin, "platform_admin", include_others_personal=True)
    # Beate shares her space with the admin → now visible, at the level she chose
    with get_conn() as conn:
        conn.execute("INSERT INTO space_members (space_id, user_id, level) VALUES (?, ?, 'read')", (beate_personal, admin))
        conn.commit()
    assert beate_personal in S.user_visible_space_ids(admin, "platform_admin")
    assert S.user_space_level(admin, beate_personal, "platform_admin") == "read"
