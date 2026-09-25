"""Phase 12: set_document_visibility skill — owner check + Paperless wrapper.

The HTTP route /api/documents/-N/visibility has the same gate. The
owner of a document decides who sees it; nobody else, an admin
included (audit 2026-09-22, 1.17 and 1.18 — until then the owner
branch read a key the creds helper never carried, and admins bypassed).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import seed_user


def _mk_ctx(*, role: str, user_id):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=user_id)


def _person(name: str, role: str, paperless_uid: int | None):
    """A seeded Yorik account with its Paperless user id on the profile."""
    from backend.database import get_conn
    uid = seed_user(name=name, role=role, email=f"{name.lower()}@example.local", password="pytestpw123")
    if paperless_uid is not None:
        with get_conn() as conn:
            conn.execute("UPDATE user_profiles SET paperless_user_id = ? WHERE id = ?", (paperless_uid, uid)); conn.commit()
    return uid


def _stub_settings_with_paperless():
    """connectors.paperless._settings() must return a non-empty api_key
    for the skill to proceed past the configured-check."""
    return {"base_url": "http://paperless.local", "api_key": "test-token"}


def _fake_paperless_get(owner_paperless_uid: int, title: str = "Test Document"):
    """Builds a fake requests.get response object that mimics Paperless'
    GET /api/documents/{id}/ payload."""
    def _fake(url, headers=None, timeout=None):
        return SimpleNamespace(
            ok=True,
            json=lambda: {"owner": owner_paperless_uid, "title": title,
                          "tags": [], "id": 42},
        )
    return _fake


def _change_ok(doc_id, vis):
    return {"ok": True, "paperless_doc_id": doc_id, "visibility": vis, "tag_ids": [101]}


class TestSetDocumentVisibility:

    def test_owner_can_change_own_document(self, fresh_app):
        """The doc is owned by paperless_uid=7; the Yorik user has that
        paperless_user_id on their profile."""
        from backend.skills.set_document_visibility.skill import execute
        beate = _person("Beate", "member", 7)
        with patch("backend.connectors.paperless._settings", side_effect=_stub_settings_with_paperless), \
             patch("backend.skills.set_document_visibility.skill.requests.get",
                   side_effect=_fake_paperless_get(owner_paperless_uid=7)), \
             patch("backend.paperless_visibility.change_document_visibility", side_effect=_change_ok):
            result = asyncio.run(execute(ctx=_mk_ctx(role="member", user_id=beate), document_id=42, visibility="business"))
            assert result["visibility"] == "business" and result["document_id"] == 42

    def test_non_owner_member_blocked(self, fresh_app):
        """Caller is paperless_uid=5; doc is owned by paperless_uid=7."""
        from backend.skills.set_document_visibility.skill import execute
        from backend.calendars import RowOwnerPermissionError
        anna = _person("Anna", "member", 5)
        with patch("backend.connectors.paperless._settings", side_effect=_stub_settings_with_paperless), \
             patch("backend.skills.set_document_visibility.skill.requests.get",
                   side_effect=_fake_paperless_get(owner_paperless_uid=7, title="someone-else-mietvertrag")):
            with pytest.raises(RowOwnerPermissionError) as excinfo:
                asyncio.run(execute(ctx=_mk_ctx(role="member", user_id=anna), document_id=42, visibility="shared"))
            # The refusal names the number, never the title: the lookup
            # ran with the admin token, so Anna may not even see the
            # document (audit 2026-09-25, L13).
            assert "someone-else-mietvertrag" not in str(excinfo.value)
            assert "42" in str(excinfo.value)

    def test_an_admin_does_not_publish_someone_elses_document(self, fresh_app):
        from backend.skills.set_document_visibility.skill import execute
        from backend.calendars import RowOwnerPermissionError
        dirk = _person("Dirk", "platform_admin", 8)
        with patch("backend.connectors.paperless._settings", side_effect=_stub_settings_with_paperless), \
             patch("backend.skills.set_document_visibility.skill.requests.get",
                   side_effect=_fake_paperless_get(owner_paperless_uid=7)):
            with pytest.raises(RowOwnerPermissionError):
                asyncio.run(execute(ctx=_mk_ctx(role="platform_admin", user_id=dirk), document_id=42, visibility="shared"))

    def test_invalid_visibility_rejected(self, fresh_app):
        from backend.skills.set_document_visibility.skill import execute
        dirk = _person("Dirk", "platform_admin", 8)
        with pytest.raises(ValueError, match="visibility must be one of"):
            asyncio.run(execute(ctx=_mk_ctx(role="platform_admin", user_id=dirk), document_id=42, visibility="public"))

    def test_paperless_not_configured_errors(self, fresh_app):
        from backend.skills.set_document_visibility.skill import execute
        dirk = _person("Dirk", "platform_admin", 8)
        with patch("backend.connectors.paperless._settings",
                   return_value={"base_url": "http://paperless.local", "api_key": ""}):
            with pytest.raises(RuntimeError, match="Paperless is not configured"):
                asyncio.run(execute(ctx=_mk_ctx(role="platform_admin", user_id=dirk), document_id=42, visibility="shared"))

    def test_document_not_found_errors(self, fresh_app):
        from backend.skills.set_document_visibility.skill import execute
        dirk = _person("Dirk", "platform_admin", 8)
        def _not_found(url, headers=None, timeout=None):
            return SimpleNamespace(ok=False, status_code=404, json=lambda: {})
        with patch("backend.connectors.paperless._settings", side_effect=_stub_settings_with_paperless), \
             patch("backend.skills.set_document_visibility.skill.requests.get", side_effect=_not_found):
            with pytest.raises(ValueError, match="not found in Paperless"):
                asyncio.run(execute(ctx=_mk_ctx(role="platform_admin", user_id=dirk), document_id=999, visibility="shared"))
