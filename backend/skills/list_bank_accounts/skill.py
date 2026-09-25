"""list_bank_accounts — the calling user's own connected bank accounts,
per backend/spaces.row_filter (own + shared, never someone else's
private account)."""

from __future__ import annotations

from typing import Any


async def execute(ctx) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        return {"_llm_hint": "No signed-in user — cannot list bank accounts."}

    from backend import spaces
    from backend.database import get_conn

    frag, params = spaces.row_filter(user_id, getattr(ctx, "role", None), "bank_accounts")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, display_name, iban, space_id, last_synced_at, last_sync_error "
            f"FROM bank_accounts WHERE {frag} ORDER BY id",
            params,
        ).fetchall()

    accounts = [dict(r) for r in rows]
    if not accounts:
        return {"accounts": [], "_llm_hint": "No bank account connected yet. Point the user at "
                                              "Settings → Finance to add one — the PIN goes "
                                              "directly into that form, never through chat."}
    hint = f"{len(accounts)} account(s). Mention which are shared (space_id set) vs private."
    return {"accounts": accounts, "_llm_hint": hint}
