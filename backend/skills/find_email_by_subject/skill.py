"""find_email_by_subject — LLM-internal resolver from subject/sender → message_id.

No email-scoped search returns LLM-visible rows today: email_briefing
summarises and universal_search is semantic-everything that often
misses keyword subject matches. This skill is the targeted resolver,
mirroring find_task_by_title / find_event_by_title in shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


async def execute(
    ctx,
    query: str,
    include_sent: bool = True,
    days_back: int = 90,
    limit: int = 10,
) -> dict[str, Any]:
    from backend.database import get_conn

    q = (query or "").strip()
    if not q:
        return {"matches": [], "count": 0}
    days_back = max(0, int(days_back or 90))
    limit     = max(1, min(int(limit or 10), 50))

    user_id = getattr(ctx, "user_id", 1)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

    # Token-AND match across subject AND from_email — one shot covers
    # "the support thread" (subject hit) and "from anna@…" (sender
    # hit). Same pattern as the other find_*_by_title resolvers.
    tokens = [t for t in q.split() if t]
    token_clauses = []
    params: list[Any] = []
    for tok in tokens:
        token_clauses.append("(lower(subject) LIKE ? OR lower(from_email) LIKE ?)")
        like = f"%{tok.lower()}%"
        params.extend([like, like])
    where = " AND ".join(token_clauses)

    sent_clause = "" if include_sent else " AND is_sent = 0"

    sql = (
        "SELECT id, from_email, subject, date_received, is_sent "
        "FROM email_messages "
        f"WHERE owner_user_id = ? AND date_received >= ? AND {where}{sent_clause} "
        "ORDER BY date_received DESC LIMIT ?"
    )
    params = [user_id, cutoff, *params, limit]

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    return {"matches": rows, "count": len(rows)}
