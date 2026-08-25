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


IDS: dict[str, str] = {}


@pytest.fixture
def two_user_db(fresh_app):
    """Seed an admin and a member (UUID ids in IDS["admin"] / IDS["member"])
    and one contact owned by each. Returns (admin_contact_id, member_contact_id)."""
    from tests.conftest import seed_user
    from backend.database import get_conn
    IDS["admin"] = seed_user(name="Admin", role="admin", email="admin@example.com")
    IDS["member"] = seed_user(name="Member", role="member", email="member@example.com")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO contacts (display_name, kind, created_by_user_id) "
            "VALUES (?, 'person', ?)",
            ("AdminContact", IDS["admin"]),
        )
        admin_contact_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO contacts (display_name, kind, created_by_user_id) "
            "VALUES (?, 'person', ?)",
            ("MemberContact", IDS["member"]),
        )
        member_contact_id = cur.lastrowid
        conn.commit()
    return admin_contact_id, member_contact_id


def _mk_ctx(*, role: str, user_id: str):
    """Minimal SkillContext stand-in — the gate only reads .role and .user_id."""
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=user_id)


class TestUpdateContact:
    def test_member_can_update_own_contact(self, two_user_db):
        """Member updating a contact THEY created is allowed."""
        _, member_contact_id = two_user_db
        from backend.skills.update_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="member", user_id=IDS["member"]),
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
                ctx=_mk_ctx(role="member", user_id=IDS["member"]),
                contact_id=admin_contact_id,
                notes="member shouldn't be able to do this",
            ))

    def test_admin_cannot_update_members_personal_contact(self, two_user_db):
        """Phase B: the household admin is bound by spaces like everyone
        else — a member's personal contact is off-limits unless shared."""
        _, member_contact_id = two_user_db
        from backend.skills.update_contact.skill import execute
        from backend.calendars import RowOwnerPermissionError
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(execute(
                ctx=_mk_ctx(role="admin", user_id=IDS["admin"]),
                contact_id=member_contact_id,
                notes="admin override",
            ))


class TestDeleteContact:
    def test_member_can_delete_own_contact(self, two_user_db):
        _, member_contact_id = two_user_db
        from backend.skills.delete_contact.skill import execute
        from backend import pending_actions as pa
        from backend.contacts import get
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="member", user_id=IDS["member"]),
            contact_id=member_contact_id,
        ))
        # Staged, not applied: the contact is still there until confirmed.
        assert result["pending"] is True
        assert get(member_contact_id, role="member", user_id=IDS["member"]) is not None
        applied = pa.apply(result["pending_id"])
        assert applied["applied"] == "delete_contact"
        assert get(member_contact_id, role="member", user_id=IDS["member"]) is None

    def test_member_cannot_delete_other_users_contact(self, two_user_db):
        admin_contact_id, _ = two_user_db
        from backend.skills.delete_contact.skill import execute
        from backend.calendars import RowOwnerPermissionError
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(execute(
                ctx=_mk_ctx(role="member", user_id=IDS["member"]),
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
                ctx=_mk_ctx(role="member", user_id=IDS["member"]),
                contact_id=admin_contact_id,
                kind="phone",
                value="+490000000",
                label="injected",
            ))


class TestRoleBasedSharing:
    """allowed_roles on a contact opens access to anyone with a matching role."""

    def test_member_blocked_when_allowed_roles_admin_only(self, two_user_db):
        """Phase 9.2 behaviour preserved: default allowed_roles='admin'
        keeps the contact private to its owner + admin."""
        admin_contact_id, _ = two_user_db
        from backend.skills.update_contact.skill import execute
        from backend.calendars import RowOwnerPermissionError
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(execute(
                ctx=_mk_ctx(role="member", user_id=IDS["member"]),
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
            ctx=_mk_ctx(role="admin", user_id=IDS["admin"]),
            contact_id=admin_contact_id,
            with_user_id=IDS["member"],
            can_edit=True,
        ))
        result = asyncio.run(upd(
            ctx=_mk_ctx(role="member", user_id=IDS["member"]),
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
            ctx=_mk_ctx(role="admin", user_id=IDS["admin"]),
            contact_id=admin_contact_id,
            with_user_id=IDS["member"],
            can_edit=False,
        ))
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(upd(
                ctx=_mk_ctx(role="member", user_id=IDS["member"]),
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
            ctx=_mk_ctx(role="admin", user_id=IDS["admin"]),
            contact_id=admin_contact_id,
            with_user_id=IDS["member"],
            can_edit=True,
        ))
        # First update works
        asyncio.run(upd(
            ctx=_mk_ctx(role="member", user_id=IDS["member"]),
            contact_id=admin_contact_id,
            notes="works",
        ))
        # Unshare
        result = asyncio.run(unshare(
            ctx=_mk_ctx(role="admin", user_id=IDS["admin"]),
            contact_id=admin_contact_id,
            with_user_id=IDS["member"],
        ))
        assert result["removed"] is True
        # Now blocked
        with pytest.raises(RowOwnerPermissionError):
            asyncio.run(upd(
                ctx=_mk_ctx(role="member", user_id=IDS["member"]),
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
            ctx=_mk_ctx(role="member", user_id=IDS["member"]),
            query="",  # list all
        ))
        ids = {c["id"] for c in result["contacts"]}
        assert member_contact_id in ids
        assert admin_contact_id not in ids

    def test_admin_does_not_see_members_personal_contacts(self, two_user_db):
        """Phase B: a member's personal space is private even from the
        household admin. Only platform_admin (infrastructure) sees all."""
        admin_contact_id, member_contact_id = two_user_db
        from backend.skills.find_contact.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="admin", user_id=IDS["admin"]),
            query="",
        ))
        ids = {c["id"] for c in result["contacts"]}
        assert admin_contact_id in ids
        assert member_contact_id not in ids
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="platform_admin", user_id=IDS["admin"]),
            query="",
        ))
        ids = {c["id"] for c in result["contacts"]}
        assert member_contact_id in ids

    def test_get_by_id_returns_none_when_inaccessible(self, two_user_db):
        """contacts.get with role+user_id returns None for hidden rows,
        same shape as 'not found' — prevents probing for the existence
        of private contacts via id enumeration."""
        admin_contact_id, member_contact_id = two_user_db
        from backend.contacts import get
        # Member can't see admin's contact
        assert get(admin_contact_id, role="member", user_id=IDS["member"]) is None
        # Owner can see their own
        assert get(admin_contact_id, role="admin", user_id=IDS["admin"]) is not None
        # Admin can't see the member's personal contact either (Phase B)
        assert get(member_contact_id, role="admin", user_id=IDS["admin"]) is None

    def test_list_contacts_for_picking_respects_visibility(self, two_user_db):
        admin_contact_id, member_contact_id = two_user_db
        from backend.skills.list_contacts_for_picking.skill import execute
        result = asyncio.run(execute(
            ctx=_mk_ctx(role="member", user_id=IDS["member"]),
        ))
        ids = {c["id"] for c in result["contacts"]}
        assert member_contact_id in ids
        assert admin_contact_id not in ids
