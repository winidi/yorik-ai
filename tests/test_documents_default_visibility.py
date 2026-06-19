"""Phase 12.1: documents_default_visibility household setting.

The upload path resolves visibility in this precedence:
  1. explicit ?visibility= query param
  2. user_profiles.default_doc_visibility (per-user)
  3. household_settings.documents_default_visibility (per-tenant — Phase 12.1)
  4. 'private' hardcoded fallback

This test exercises the household-level fallback in isolation: when
the user has no per-user default, the tenant setting kicks in.
"""
from __future__ import annotations

import pytest


def test_migration_seeds_documents_default_visibility(fresh_app):
    """Migration 032 inserts the row with default 'private'."""
    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM household_settings WHERE key = ?",
            ("documents_default_visibility",),
        ).fetchone()
    assert row is not None, "migration 032 did not run"
    assert row["value"] == "private"


def test_get_setting_returns_seeded_default(fresh_app):
    """The household_settings module reads the seeded value."""
    from backend.household_settings import get_setting
    val = get_setting("documents_default_visibility", default="<missing>")
    assert val == "private"


def test_household_admin_can_flip_default(fresh_app):
    """The whole point of the setting: a family flips it to 'shared'."""
    from backend.database import get_conn
    from backend.household_settings import set_setting, get_setting
    # Seed a user_profiles row so the FK on updated_by_user_id holds.
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_profiles "
            "(id, name, role, voice_id, email) VALUES (1, 'Admin', 'admin', 'admin', 'a@x')"
        )
        conn.commit()
    set_setting("documents_default_visibility", "shared", updated_by_user_id=1)
    assert get_setting("documents_default_visibility") == "shared"


def test_household_default_falls_back_to_caller_default_when_unset(fresh_app):
    """get_setting respects the caller's default when the key is missing."""
    from backend.household_settings import get_setting
    val = get_setting("documents_default_THIS_KEY_DOES_NOT_EXIST",
                       default="private")
    assert val == "private"
