"""whatsapp_briefing skill — wraps the existing /api/whatsapp/briefing implementation."""

from __future__ import annotations
from typing import Any


async def execute(ctx, hours: int = 24) -> dict[str, Any]:
    hours = max(1, min(int(hours), 168))
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        raise ValueError("whatsapp_briefing requires ctx.user_id")
    from backend.whatsapp import briefing
    # `briefing` is a FastAPI route — calling it as a function needs the
    # `user` Depends parameter passed explicitly, otherwise it stays as
    # the unresolved Depends sentinel and crashes on user["id"].
    return await briefing(hours=hours, user={"id": user_id})
