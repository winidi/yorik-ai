"""list_email_accounts: the caller's own email accounts and which
provider each is with (email_providers.provider_name). Read-only; no
passwords, hosts or user names leave it."""
from __future__ import annotations

from typing import Any


def own_accounts(user_id: str) -> list[dict[str, Any]]:
    from backend.database import get_conn
    from backend.email_providers import provider_name
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT email, display_name, imap_host, imap_port, enabled, is_default, last_sync_at, last_error "
            "FROM email_accounts WHERE owner_user_id = ? ORDER BY is_default DESC, id", (user_id,)).fetchall()
    return [{
        "email": r["email"],
        "name": r["display_name"] or "",
        "provider": provider_name(r["email"], r["imap_host"], r["imap_port"]),
        "is_default": bool(r["is_default"]),
        "enabled": bool(r["enabled"]),
        "last_sync_at": str(r["last_sync_at"]) if r["last_sync_at"] else None,
        "last_error": r["last_error"] or None,
    } for r in rows]


async def execute(ctx) -> dict[str, Any]:
    from backend.skills.registry import require_user_id
    accounts = own_accounts(str(require_user_id(ctx)))
    hint = ("To send from one of these, pass its email as from_email to prepare_email. "
            "Mail reaches Yorik only through these accounts.")
    if not accounts:
        hint = "No email account is connected in Yorik; the user adds one in Yorik's Email settings."
    return {"accounts": accounts, "count": len(accounts), "_llm_hint": hint}
