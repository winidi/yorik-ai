"""find_event_by_title — LLM-internal resolver from title → event_id.

Complement to check_calendar (which renders cards but hides titles +
IDs from the LLM). Returns minimal rows for the LLM to act on with
update_calendar_event / delete_calendar_event.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


async def execute(
    ctx,
    query: str,
    days_back: int = 30,
    days_forward: int = 90,
    limit: int = 10,
) -> dict[str, Any]:
    from backend.database import get_conn

    q = (query or "").strip()
    if not q:
        return {"matches": [], "count": 0}
    days_back    = max(0, int(days_back or 30))
    days_forward = max(0, int(days_forward or 90))
    limit        = max(1, min(int(limit or 10), 50))

    now   = datetime.now(timezone.utc)
    start = (now - timedelta(days=days_back)).isoformat()
    end   = (now + timedelta(days=days_forward)).isoformat()

    # Token-AND matching — see find_task_by_title for the rationale.
    tokens = [t for t in q.split() if t]
    where_extra = " AND ".join("lower(title) LIKE ?" for _ in tokens)
    sql = (
        "SELECT id, title, starts_at, ends_at, all_day "
        "FROM events "
        "WHERE starts_at >= ? AND starts_at <= ? AND " + where_extra + " "
        "ORDER BY starts_at ASC LIMIT ?"
    )
    params = [start, end, *(f"%{t.lower()}%" for t in tokens), limit]

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    return {"matches": rows, "count": len(rows)}
