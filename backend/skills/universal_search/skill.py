"""universal_search skill — same dispatch as /api/search."""

from __future__ import annotations
from typing import Any


async def execute(ctx, query: str) -> dict[str, Any]:
    if not query or not query.strip():
        return {"query": query, "total": 0, "results": {}}
    user_id = getattr(ctx, "user_id", 1)

    # Reuse the route's internals so the skill and the HTTP endpoint
    # return identical shapes.
    from backend.search_routes import universal_search
    # Build a fake user dict matching what current_user gives back.
    fake_user = {"id": user_id, "role": getattr(ctx, "role", "admin")}
    return await universal_search(q=query, user=fake_user)
