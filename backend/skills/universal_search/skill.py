"""universal_search skill — same dispatch as /api/search."""

from __future__ import annotations

import asyncio
from typing import Any, Optional


async def execute(ctx, query: str, also: Optional[list] = None) -> dict[str, Any]:
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
    from backend.agent.prefetch import for_model
    # The model's other wordings (synonyms, another language, the
    # company behind a product) run next to the query; the query's own
    # hits come first. "Claude" never reached the Anthropic receipt,
    # which does not name Claude (document test set 2026-09-27).
    variants = [v.strip() for v in (also or []) if isinstance(v, str) and v.strip()
                and v.strip().lower() != query.strip().lower()][:3]
    runs = await asyncio.gather(*(universal_search(q=q, user=fake_user) for q in [query, *variants]))
    from backend.agent.prefetch import merge
    raw = merge(list(runs))
    if variants:
        raw["also_searched"] = variants
    return for_model(raw, " ".join([query, *variants]), per_source=5)
