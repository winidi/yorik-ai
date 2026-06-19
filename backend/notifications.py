"""In-app notifications — the bell icon's data source.

Notifications are kind-tagged so the frontend can render + route them
differently per kind. Current kinds:

  task_assigned   — Anna assigned a task to you
  task_status     — assignee accepted / declined / completed your task
  (more later: event_invite, wa_priority, backup_failed, …)

Every notification carries an optional `navigate_to` (relative URL)
that clicking the bell row opens — usually deep-links into the React
shell at the right view.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .database import get_conn

log = logging.getLogger("yorik.notifications")


def create(
    user_id: str,
    kind: str,
    title: str,
    body: Optional[str] = None,
    payload: Optional[dict] = None,
    navigate_to: Optional[str] = None,
) -> int:
    """Insert one notification row for `user_id`. Returns the new id.
    Caller doesn't care about it most of the time; the bell polls."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO notifications (user_id, kind, title, body, payload_json, navigate_to) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, kind, title, body,
             json.dumps(payload) if payload else None,
             navigate_to),
        )
        conn.commit()
    return cur.lastrowid


def list_for_user(user_id: str, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    sql = ("SELECT id, kind, title, body, payload_json, navigate_to, "
           "       is_read, read_at, created_at "
           "FROM notifications WHERE user_id=?")
    params: list[Any] = [user_id]
    if unread_only:
        sql += " AND is_read=0"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d.pop("payload_json") or "null")
        except json.JSONDecodeError:
            d["payload"] = None
        d["is_read"] = bool(d["is_read"])
        out.append(d)
    return out


def unread_count(user_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE user_id=? AND is_read=0",
            (user_id,),
        ).fetchone()
    return row["n"] or 0


def mark_read(user_id: str, ids: list[int]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE notifications SET is_read=1, read_at=datetime('now') "
            f"WHERE user_id=? AND id IN ({placeholders})",
            [user_id, *ids],
        )
        conn.commit()
    return cur.rowcount or 0


def mark_all_read(user_id: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notifications SET is_read=1, read_at=datetime('now') "
            "WHERE user_id=? AND is_read=0",
            (user_id,),
        )
        conn.commit()
    return cur.rowcount or 0
