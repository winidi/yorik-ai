"""Phase 9.2: members can only mutate contacts they own.

Same ownership pattern Phase 6 added for tasks + calendar events,
now applied to the six contact write skills (update_contact,
delete_contact, add_contact_channel, add_contact_address,
mark_contact_spam, promote_pending_contact).

Why: the audit identified that any [admin, member] member could
silently edit any other member's contacts. Fine for a household
where the contacts are shared, broken for any business where
employees keep their own client lists.

These tests exercise update_contact + delete_contact (the two most
common); the helper is identical across all six callers so coverage
of two is enough to gate the regression.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def two_user_db(fresh_app):
    """Insert two users (admin id=1, member id=2) and two contacts
    (one owned by each). Returns (admin_contact_id, member_contact_id)."""
    from backend.database import get_conn
    with get_conn() as conn:
        # The fresh_app fixture's setup may have already inserted user_id=1
        # as the seeded admin. Make sure a member exists too.
        conn.execute(
            "INSERT OR IGNORE INTO user_profiles "
            "(id, name, role, voice_id, email) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "Admin", "admin", "admin", "admin@example.com"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO user_profiles "
            "(id, name, role, voice_id, email) "
            "VALUES (?, ?, ?, ?, ?)",
            (2, "Member", "member", "member", "member@example.com"),
        )
        cur = conn.execute(
            "INSERT INTO contacts (display_name, kind, created_by_user_id) "
            "VALUES (?, 'person', 1)",
            ("AdminContact",),
        )
        admin_contact_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO contacts (display_name, kind, created_by_user_id) "
            "VALUES (?, 'person', 2)",
            ("MemberContact",),
        )
        member_contact_id = cur.lastrowid
        conn.commit()
    return admin_contact_id, member_contact_id


def _mk_ctx(*, role: str, user_id: int):
    """Minimal SkillContext stand-in — the gate only reads .role and .user_id."""
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=user_id)


class TestUpdateContact:
    def test_member_can_update_own_contact(self, two_user_db):
        """Member updating a contact THEY created is allowed."""
        _, member_contact_id = two_user_db
        from backend.skills.update_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="member", user_id=2),
            contact_id=member_contact_id,
            notes="updated by member",
        ))
        assert result["contact"]["notes"] == "updated by member"

    def test_member_cannot_update_other_users_contact(self, two_user_db):
        """Member trying to update someone else's contact must hit the gate."""
        admin_contact_id, _ = two_user_db
        from backend.skills.update_contact.skill import execute
        from backend.calendars import RowOwnerPermissionError
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(execute(
                ctx=_mk_ctx(role="member", user_id=2),
                contact_id=admin_contact_id,
                notes="member shouldn't be able to do this",
            ))

    def test_admin_can_update_any_contact(self, two_user_db):
        """Admin role bypasses the ownership gate."""
        _, member_contact_id = two_user_db
        from backend.skills.update_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="admin", user_id=1),
            contact_id=member_contact_id,
            notes="admin override",
        ))
        assert result["contact"]["notes"] == "admin override"


class TestDeleteContact:
    def test_member_can_delete_own_contact(self, two_user_db):
        _, member_contact_id = two_user_db
        from backend.skills.delete_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="member", user_id=2),
            contact_id=member_contact_id,
        ))
        assert result["deleted_contact_id"] == member_contact_id

    def test_member_cannot_delete_other_users_contact(self, two_user_db):
        admin_contact_id, _ = two_user_db
        from backend.skills.delete_contact.skill import execute
        from backend.calendars import RowOwnerPermissionError
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(execute(
                ctx=_mk_ctx(role="member", user_id=2),
                contact_id=admin_contact_id,
            ))


class TestAddContactChannel:
    def test_member_cannot_add_channel_to_other_users_contact(self, two_user_db):
        """A member shouldn't be able to slip a phone number onto someone
        else's customer record."""
        admin_contact_id, _ = two_user_db
        from backend.skills.add_contact_channel.skill import execute
        from backend.calendars import RowOwnerPermissionError
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(execute(
                ctx=_mk_ctx(role="member", user_id=2),
                contact_id=admin_contact_id,
                kind="phone",
                value="+490000000",
                label="injected",
            ))


class TestRoleBasedSharing:
    """allowed_roles on a contact opens access to anyone with a matching role."""

    def test_member_can_edit_when_contact_allows_member_role(self, two_user_db):
        admin_contact_id, _ = two_user_db
        from backend.database import get_conn
        with get_conn() as conn:
            conn.execute(
                "UPDATE contacts SET allowed_roles = ? WHERE id = ?",
                ("admin,member", admin_contact_id),
            )
            conn.commit()
        from backend.skills.update_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="member", user_id=2),
            contact_id=admin_contact_id,
            notes="now editable by any member",
        ))
        assert result["contact"]["notes"] == "now editable by any member"

    def test_member_blocked_when_allowed_roles_admin_only(self, two_user_db):
        """Phase 9.2 behaviour preserved: default allowed_roles='admin'
        keeps the contact private to its owner + admin."""
        admin_contact_id, _ = two_user_db
        from backend.skills.update_contact.skill import execute
        from backend.calendars import RowOwnerPermissionError
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(execute(
                ctx=_mk_ctx(role="member", user_id=2),
                contact_id=admin_contact_id,
                notes="should fail",
            ))


class TestPerUserSharing:
    """Per-user shares grant access to a specific user_id."""

    def test_share_grants_edit_access(self, two_user_db):
        admin_contact_id, _ = two_user_db
        from backend.skills.share_contact.skill import execute as share
        from backend.skills.update_contact.skill import execute as upd
        asyncio.run(share(
            ctx=_mk_ctx(role="admin", user_id=1),
            contact_id=admin_contact_id,
            with_user_id=2,
            can_edit=True,
        ))
        result = asyncio.run(upd(
            ctx=_mk_ctx(role="member", user_id=2),
            contact_id=admin_contact_id,
            notes="member updated via share",
        ))
        assert result["contact"]["notes"] == "member updated via share"

    def test_share_view_only_blocks_edit(self, two_user_db):
        admin_contact_id, _ = two_user_db
        from backend.skills.share_contact.skill import execute as share
        from backend.skills.update_contact.skill import execute as upd
        from backend.calendars import RowOwnerPermissionError
        asyncio.run(share(
            ctx=_mk_ctx(role="admin", user_id=1),
            contact_id=admin_contact_id,
            with_user_id=2,
            can_edit=False,
        ))
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(upd(
                ctx=_mk_ctx(role="member", user_id=2),
                contact_id=admin_contact_id,
                notes="should still fail",
            ))

    def test_unshare_revokes_access(self, two_user_db):
        admin_contact_id, _ = two_user_db
        from backend.skills.share_contact.skill import execute as share
        from backend.skills.unshare_contact.skill import execute as unshare
        from backend.skills.update_contact.skill import execute as upd
        from backend.calendars import RowOwnerPermissionError

        asyncio.run(share(
            ctx=_mk_ctx(role="admin", user_id=1),
            contact_id=admin_contact_id,
            with_user_id=2,
            can_edit=True,
        ))
        # First update works
        asyncio.run(upd(
            ctx=_mk_ctx(role="member", user_id=2),
            contact_id=admin_contact_id,
            notes="works",
        ))
        # Unshare
        result = asyncio.run(unshare(
            ctx=_mk_ctx(role="admin", user_id=1),
            contact_id=admin_contact_id,
            with_user_id=2,
        ))
        assert result["removed"] is True
        # Now blocked
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(upd(
                ctx=_mk_ctx(role="member", user_id=2),
                contact_id=admin_contact_id,
                notes="should fail after unshare",
            ))


class TestReadVisibility:
    """Phase 9.4: find_contact / list_contacts_for_picking must filter
    rows by what the caller can see. Without this, the read path is a
    backdoor around the write gate — a member would see every private
    contact in the household."""

    def test_member_sees_only_their_own_contacts_by_default(self, two_user_db):
        admin_contact_id, member_contact_id = two_user_db
        from backend.skills.find_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="member", user_id=2),
            query="",  # list all
        ))
        ids = {c["id"] for c in result["contacts"]}
        assert member_contact_id in ids
        assert admin_contact_id not in ids

    def test_admin_sees_everything(self, two_user_db):
        admin_contact_id, member_contact_id = two_user_db
        from backend.skills.find_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="admin", user_id=1),
            query="",
        ))
        ids = {c["id"] for c in result["contacts"]}
        assert admin_contact_id in ids
        assert member_contact_id in ids

    def test_role_allowlist_grants_visibility(self, two_user_db):
        """A contact with allowed_roles='admin,member' should surface
        in any member's find_contact, not just the owner's."""
        admin_contact_id, _ = two_user_db
        from backend.database import get_conn
        with get_conn() as conn:
            conn.execute(
                "UPDATE contacts SET allowed_roles = ? WHERE id = ?",
                ("admin,member", admin_contact_id),
            )
            conn.commit()
        from backend.skills.find_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="member", user_id=2),
            query="",
        ))
        ids = {c["id"] for c in result["contacts"]}
        assert admin_contact_id in ids

    def test_per_user_share_grants_visibility(self, two_user_db):
        admin_contact_id, _ = two_user_db
        from backend.skills.share_contact.skill import execute as share
        asyncio.run(share(
            ctx=_mk_ctx(role="admin", user_id=1),
            contact_id=admin_contact_id,
            with_user_id=2,
            can_edit=False,  # view-only is enough for reads
        ))
        from backend.skills.find_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="member", user_id=2),
            query="",
        ))
        ids = {c["id"] for c in result["contacts"]}
        assert admin_contact_id in ids

    def test_get_by_id_returns_none_when_inaccessible(self, two_user_db):
        """contacts.get with role+user_id returns None for hidden rows,
        same shape as 'not found' — prevents probing for the existence
        of private contacts via id enumeration."""
        admin_contact_id, _ = two_user_db
        from backend.contacts import get
        # Member can't see admin's contact
        assert get(admin_contact_id, role="member", user_id=2) is None
        # Admin can
        assert get(admin_contact_id, role="admin", user_id=1) is not None
        # Owner can see their own
        assert get(admin_contact_id, role="admin", user_id=1) is not None

    def test_list_contacts_for_picking_respects_visibility(self, two_user_db):
        admin_contact_id, member_contact_id = two_user_db
        from backend.skills.list_contacts_for_picking.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="member", user_id=2),
        ))
        ids = {c["id"] for c in result["contacts"]}
        assert member_contact_id in ids
        assert admin_contact_id not in ids


class TestHouseholdDefault:
    """Per-tenant default for new contacts' allowed_roles."""

    def test_default_admin_keeps_new_contact_private(self, two_user_db):
        from backend.skills.add_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="admin", user_id=1),
            display_name="DefaultContact",
        ))
        assert result["contact"]["allowed_roles"] == "admin"

    def test_custom_default_applied_to_new_contact(self, two_user_db):
        from backend.household_settings import set_setting
        set_setting("contacts_default_allowed_roles", "admin,member,child")
        from backend.skills.add_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="admin", user_id=1),
            display_name="SharedContact",
        ))
        assert result["contact"]["allowed_roles"] == "admin,member,child"

    def test_explicit_allowed_roles_overrides_default(self, two_user_db):
        from backend.household_settings import set_setting
        set_setting("contacts_default_allowed_roles", "admin,member,child")
        from backend.skills.add_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="admin", user_id=1),
            display_name="OverriddenContact",
            allowed_roles="admin",
        ))
        assert result["contact"]["allowed_roles"] == "admin"
