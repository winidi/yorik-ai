"""Tasks retriever — open tasks mentioning the contact by name.

Lets the LLM see "user has 'Send contract to Anna' open" when Anna
emails — drives the confirm_task_done suggestion (later phase) and
gives context for current replies ("yes, sending the contract now").

Filters to undone tasks; done tasks aren't actionable as suggestions.
"""

from __future__ import annotations

from ..registry import ContextRetriever, Evidence, RetrieverContext, register_retriever


async def _fetch(ctx: RetrieverContext) -> list[Evidence]:
    if ctx.contact_id is None:
        return []
    from ...database import get_conn
    with get_conn() as conn:
        name_row = conn.execute(
            "SELECT display_name FROM contacts WHERE id=?",
            (ctx.contact_id,),
        ).fetchone()
    if not name_row:
        return []
    name = (name_row["display_name"] or "").strip()
    if not name:
        return []

    pattern = f"%{name}%"
    from ... import spaces as _sp
    with get_conn() as conn:
        # the owner's own tasks and the ones shared with them (audit 2026-09-22, 3.6)
        role_row = conn.execute("SELECT role FROM user_profiles WHERE id = ?", (ctx.owner_user_id,)).fetchone()
        vis_sql, vis_params = _sp.row_filter(ctx.owner_user_id, (role_row["role"] if role_row else "") or "", "tasks")
        rows = conn.execute(
            "SELECT id, title, due_date, person "
            "FROM tasks "
            "WHERE done = 0 "
            "  AND (LOWER(title) LIKE LOWER(?) "
            "       OR LOWER(COALESCE(person,'')) = LOWER(?) "
            "       OR LOWER(COALESCE(notes,'')) LIKE LOWER(?)) "
            f"  AND {vis_sql} "
            "ORDER BY due_date NULLS LAST, id DESC LIMIT 5",
            (pattern, name, pattern, *vis_params),
        ).fetchall()

    out: list[Evidence] = []
    for r in rows:
        due = f" (due {r['due_date']})" if r["due_date"] else ""
        out.append(Evidence(
            kind="task",
            ref_id=int(r["id"]),
            snippet=f"{r['title']}{due}"[:140],
        ))
    return out


register_retriever(ContextRetriever(
    name="tasks",
    scope=["message"],
    fetch=_fetch,
))
