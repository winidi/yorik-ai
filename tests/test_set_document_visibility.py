"""Phase 12: set_document_visibility skill — owner check + Paperless wrapper.

The HTTP route /api/documents/-N/visibility already exists with the
same gate. This test ensures the chat-skill wrapper enforces the
gate identically — admin bypasses, owner allowed, non-owner blocked
with a clear error.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _mk_ctx(*, role: str, user_id: int):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=user_id)


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


def _stub_change_visibility(*args, **kwargs):
    return {"ok": True, "paperless_doc_id": 42, "visibility": kwargs.get("visibility") or args[1],
            "tag_ids": [101]}


class TestSetDocumentVisibility:

    def test_admin_can_change_any_document(self, fresh_app):
        from backend.skills.set_document_visibility.skill import execute
        with patch("backend.connectors.paperless._settings",
                   side_effect=_stub_settings_with_paperless), \
             patch("backend.skills.set_document_visibility.skill.requests.get",
                   side_effect=_fake_paperless_get(owner_paperless_uid=2)), \
             patch("backend.paperless_visibility.change_document_visibility",
                   side_effect=lambda doc_id, vis: {"ok": True, "paperless_doc_id": doc_id,
                                                    "visibility": vis, "tag_ids": [101]}):
            result = asyncio.run(execute(
                ctx=_mk_ctx(role="admin", user_id=1),
                document_id=42,
                visibility="shared",
            ))
            assert result["document_id"] == 42
            assert result["visibility"] == "shared"

    def test_owner_can_change_own_document(self, fresh_app):
        """The doc is owned by paperless_uid=7. The Yorik user has the
        same paperless_user_id stored."""
        from backend.skills.set_document_visibility.skill import execute
        with patch("backend.connectors.paperless._settings",
                   side_effect=_stub_settings_with_paperless), \
             patch("backend.external_users.get_user_paperless_creds",
                   return_value={"paperless_user_id": 7}), \
             patch("backend.skills.set_document_visibility.skill.requests.get",
                   side_effect=_fake_paperless_get(owner_paperless_uid=7)), \
             patch("backend.paperless_visibility.change_document_visibility",
                   side_effect=lambda doc_id, vis: {"ok": True, "paperless_doc_id": doc_id,
                                                    "visibility": vis, "tag_ids": [102]}):
            result = asyncio.run(execute(
                ctx=_mk_ctx(role="member", user_id=2),
                document_id=42,
                visibility="business",
            ))
            assert result["visibility"] == "business"

    def test_non_owner_member_blocked(self, fresh_app):
        """Caller is paperless_uid=5; doc is owned by paperless_uid=7."""
        from backend.skills.set_document_visibility.skill import execute
        from backend.calendars import RowOwnerPermissionError
        with patch("backend.connectors.paperless._settings",
                   side_effect=_stub_settings_with_paperless), \
             patch("backend.external_users.get_user_paperless_creds",
                   return_value={"paperless_user_id": 5}), \
             patch("backend.skills.set_document_visibility.skill.requests.get",
                   side_effect=_fake_paperless_get(owner_paperless_uid=7, title="someone-else-mietvertrag")):
            with pytest.raises(RowOwnerPermissionError) as excinfo:
                asyncio.run(execute(
                    ctx=_mk_ctx(role="member", user_id=2),
                    document_id=42,
                    visibility="shared",
                ))
            # Error names the doc so the user knows what was refused.
            assert "someone-else-mietvertrag" in str(excinfo.value)

    def test_invalid_visibility_rejected(self, fresh_app):
        from backend.skills.set_document_visibility.skill import execute
        with pytest.raises(ValueError, match="visibility must be one of"):
            asyncio.run(execute(
                ctx=_mk_ctx(role="admin", user_id=1),
                document_id=42,
                visibility="public",
            ))

    def test_paperless_not_configured_errors(self, fresh_app):
        from backend.skills.set_document_visibility.skill import execute
        with patch("backend.connectors.paperless._settings",
                   return_value={"base_url": "http://paperless.local", "api_key": ""}):
            with pytest.raises(RuntimeError, match="Paperless is not configured"):
                asyncio.run(execute(
                    ctx=_mk_ctx(role="admin", user_id=1),
                    document_id=42,
                    visibility="shared",
                ))

    def test_document_not_found_errors(self, fresh_app):
        from backend.skills.set_document_visibility.skill import execute
        def _not_found(url, headers=None, timeout=None):
            return SimpleNamespace(ok=False, status_code=404, json=lambda: {})
        with patch("backend.connectors.paperless._settings",
                   side_effect=_stub_settings_with_paperless), \
             patch("backend.skills.set_document_visibility.skill.requests.get",
                   side_effect=_not_found):
            with pytest.raises(ValueError, match="not found in Paperless"):
                asyncio.run(execute(
                    ctx=_mk_ctx(role="admin", user_id=1),
                    document_id=999,
                    visibility="shared",
                ))
