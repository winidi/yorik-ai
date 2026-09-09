"""find_task_by_title — LLM-internal resolver from title → task_id.

Complement to check_tasks (which renders cards but hides titles +
IDs from the LLM by design). When the user says "delete the milk
task" / "mark the gym one done", the LLM needs the task_id to act;
check_tasks won't give it. This skill does.

No UI emission. No cards. Pure LLM helper.
"""

from __future__ import annotations

from typing import Any


async def execute(
    ctx,
    query: str,
    include_done: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    from backend.database import get_conn

    q = (query or "").strip()
    if not q:
        return {"matches": [], "count": 0}
    limit = max(1, min(int(limit or 10), 50))

    # Token-AND matching: each whitespace-separated word in `query`
    # must appear somewhere in the title (case-insensitive). Single-
    # word queries fall through to plain substring. Catches the
    # "smoke2-1 bread" vs "smoke2-1 buy bread" case the strict
    # substring match dropped — words don't have to be contiguous.
    where: list[str] = []
    params: list[Any] = []
    tokens = [t for t in q.split() if t]
    for tok in tokens:
        where.append("lower(title) LIKE ?")
        params.append(f"%{tok.lower()}%")
    if not include_done:
        where.append("done = 0")

    # Scope to what the caller may see (Phase B spaces model). Without
    # this a restricted/child role could enumerate any household
    # member's rows by title. The operator is scoped like everyone else.
    role = (getattr(ctx, "role", None) or "").lower()
    uid = getattr(ctx, "user_id", None)
    if uid:
        from backend import spaces as _sp
        visible = _sp.user_visible_space_ids(uid, role, area="tasks")
        if visible:
            where.append(f"(space_id IN ({','.join('?' * len(visible))}) OR created_by_user_id = ?)")
            params.extend([*visible, uid])
        else:
            where.append("created_by_user_id = ?")
            params.append(uid)

    sql = (
        "SELECT id, title, due_date, done, parent_task_id "
        "FROM tasks WHERE " + " AND ".join(where) + " "
        "ORDER BY done ASC, due_date IS NULL, due_date ASC, id DESC "
        "LIMIT ?"
    )
    params.append(limit)

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    return {"matches": rows, "count": len(rows)}
