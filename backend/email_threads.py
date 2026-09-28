"""Which conversation a mail belongs to — the way Thunderbird does it
(Jamie Zawinski's threading): a mail joins the thread of the nearest
mail it answers that we already have, In-Reply-To first, then the
References from the newest back. Only when none is known does the root
of References (or In-Reply-To, or the mail itself) name the thread. A
parent that arrives after its answers takes them into its thread.

Until 2026-09-28 the first References entry was the thread: Oliver's
answer referenced only Dirk's question, not his own first mail, and
stood alone in the inbox while Dirk waited for it.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional


def thread_for(conn: Any, owner_user_id: Any, message_id: Optional[str], in_reply_to: Optional[str],
               references: Optional[Iterable[str]]) -> Optional[str]:
    refs = [str(r).strip("<>") for r in (references or []) if r]
    in_reply_to = (in_reply_to or "").strip("<>") or None
    for parent in [in_reply_to, *reversed(refs)]:
        if not parent or parent == message_id:
            continue
        row = conn.execute(
            "SELECT thread_id FROM email_messages WHERE owner_user_id = ? AND message_id = ? "
            "AND COALESCE(thread_id, '') <> '' LIMIT 1", (owner_user_id, parent)).fetchone()
        if row:
            return row["thread_id"]
    return (refs[0] if refs else None) or in_reply_to or message_id


def adopt_answers(conn: Any, owner_user_id: Any, message_id: Optional[str], thread_id: Optional[str]) -> int:
    """Mails already here that answer this one move, with their whole
    thread, into this one's thread."""
    if not message_id or not thread_id:
        return 0
    rows = conn.execute(
        "SELECT DISTINCT thread_id FROM email_messages WHERE owner_user_id = ? AND in_reply_to = ? "
        "AND COALESCE(thread_id, '') NOT IN ('', ?)", (owner_user_id, message_id, thread_id)).fetchall()
    moved = 0
    for r in rows:
        cur = conn.execute("UPDATE email_messages SET thread_id = ? WHERE owner_user_id = ? AND thread_id = ?",
                           (thread_id, owner_user_id, r["thread_id"]))
        moved += cur.rowcount or 0
    return moved
