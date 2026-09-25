"""universal_search skill — same dispatch as /api/search."""

from __future__ import annotations
from typing import Any


async def execute(ctx, query: str) -> dict[str, Any]:
    if not query or not query.strip():
        return {"query": query, "total": 0, "results": {}}
    from backend.skills.registry import require_user_id
    user_id = require_user_id(ctx)

    # Reuse the route's internals so the skill and the HTTP endpoint
    # return identical shapes.
    from backend.search_routes import universal_search
    # Build a fake user dict matching what current_user gives back.
    # No role is no reason to search as admin (audit 2026-09-25, pattern D).
    fake_user = {"id": user_id, "role": getattr(ctx, "role", None) or "member"}
    return await universal_search(q=query, user=fake_user)
