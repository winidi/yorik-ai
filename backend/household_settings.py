"""Per-tenant key/value settings.

Yorik is single-tenant per running instance, so 'household_settings'
is a flat key/value table — no tenant_id, no namespacing. First user:
contacts_default_allowed_roles (Phase 9.3). Future settings (default
calendar visibility, default event categories, etc.) land here too.

Schema lives in migrations/031_contact_shares_and_household_settings.sql.

Values are stored as TEXT. Callers parse the type they expect — most
settings are strings or comma-separated lists; for booleans use '0' /
'1' and bool(int(value)).
"""
from __future__ import annotations

from typing import Optional


def get_setting(key: str, *, default: Optional[str] = None) -> str:
    """Read a setting. Returns `default` (or empty string) if the key
    isn't set. Never raises — a missing household_settings row should
    not bring down a write path. Callers cope with `default`."""
    try:
        from .database import get_conn
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM household_settings WHERE key = ?",
                (key,),
            ).fetchone()
        if row is not None:
            return str(row["value"])
    except Exception:
        # Table might not exist yet (pre-migration), or DB might be
        # locked. Fall through to default — settings are advisory.
        pass
    return default if default is not None else ""


def set_setting(key: str, value: str, *,
                updated_by_user_id: Optional[int] = None) -> None:
    """Upsert. Only admins should call into the route that wraps this —
    this function trusts its caller to have done the role check."""
    from .database import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO household_settings (key, value, updated_by_user_id) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  value=excluded.value, "
            "  updated_at=datetime('now'), "
            "  updated_by_user_id=excluded.updated_by_user_id",
            (key, value, updated_by_user_id),
        )
        conn.commit()
