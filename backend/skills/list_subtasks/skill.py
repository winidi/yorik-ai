"""list_subtasks — LLM-internal resolver for parent_task_id → child rows.

Companion to find_task_by_title (which resolves a parent's id by name)
and check_tasks (which lists the user's whole open task set). Sits in
the gap: "what are the children of THIS specific task" is a common
question that check_tasks can't answer without enumerating everything.
No UI emission, no cards.
"""

from __future__ import annotations

from typing import Any


async def execute(
    ctx,
    parent_task_id: int,
    include_done: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    from backend.database import get_conn

    if not isinstance(parent_task_id, int) or parent_task_id <= 0:
        raise ValueError("parent_task_id must be a positive integer")
    limit = max(1, min(int(limit or 50), 200))

    where: list[str] = ["parent_task_id = ?"]
    params: list[Any] = [parent_task_id]
    if not include_done:
        where.append("done = 0")

    sql = (
        "SELECT id, title, due_date, done "
        "FROM tasks WHERE " + " AND ".join(where) + " "
        "ORDER BY done ASC, id ASC LIMIT ?"
    )
    params.append(limit)

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    return {"matches": rows, "count": len(rows)}
