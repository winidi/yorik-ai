"""email_briefing skill — LLM summary of recent inbound mail."""

from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from typing import Any


async def execute(ctx, hours: int = 24) -> dict[str, Any]:
    hours = max(1, min(int(hours), 168))
    user_id = getattr(ctx, "user_id", 1)
    from backend.database import get_conn
    from backend.whatsapp import _call_llm  # reuse the same llama-swap client

    since_iso = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)).isoformat(timespec="seconds")

    with get_conn() as conn:
        # Accounts the user owns.
        n_accounts = conn.execute(
            "SELECT COUNT(*) FROM email_accounts WHERE owner_user_id=? AND enabled=1",
            (user_id,),
        ).fetchone()[0]
        # Top threads with inbound mail in window, grouped by thread.
        # Postgres is strict about GROUP BY — every non-aggregated
        # column has to be aggregated or in the GROUP BY clause.
        # SQLite tolerates "any value per group"; Postgres doesn't.
        # MIN() works for picking one representative value per thread
        # without changing the semantics (these fields are roughly
        # constant per thread anyway: subject = first subject line;
        # from_email/from_name = original sender — close enough for a
        # briefing's "you have N threads" summary).
        thread_rows = conn.execute(
            "SELECT thread_id, MIN(from_email) AS from_email, "
            "       MIN(from_name) AS from_name, MIN(subject) AS subject, "
            "       MAX(date_received) AS last_ts, COUNT(*) AS msg_count "
            "FROM email_messages "
            "WHERE owner_user_id=? AND date_received >= ? AND is_sent=0 "
            "GROUP BY thread_id ORDER BY last_ts DESC LIMIT 15",
            (user_id, since_iso),
        ).fetchall()
        # Total counts.
        new_count = conn.execute(
            "SELECT COUNT(*) FROM email_messages WHERE owner_user_id=? AND date_received >= ? AND is_sent=0",
            (user_id, since_iso),
        ).fetchone()[0]
        unread_count = conn.execute(
            "SELECT COUNT(*) FROM email_messages WHERE owner_user_id=? AND is_unread=1 AND is_sent=0",
            (user_id,),
        ).fetchone()[0]
        # Which threads need a reply (inbound exists, no sent message in same thread).
        # Returns both the integer `id` (for /r/email?msg= deep-links) AND
        # the IMAP `message_id` string (for any LLM/reply-threading needs).
        needing_reply = conn.execute(
            "SELECT thread_id, MAX(id) AS id, MAX(message_id) AS message_id, "
            "       MIN(from_email) AS from_email, MIN(from_name) AS from_name, "
            "       MIN(subject) AS subject "
            "FROM email_messages m "
            "WHERE owner_user_id=? AND date_received >= ? AND is_sent=0 "
            "  AND thread_id NOT IN ("
            "    SELECT thread_id FROM email_messages "
            "    WHERE owner_user_id=? AND is_sent=1 AND date_sent >= m.date_received"
            "  ) "
            "GROUP BY thread_id ORDER BY MAX(date_received) DESC LIMIT 12",
            (user_id, since_iso, user_id),
        ).fetchall()

        # Pull the latest 3 messages of each top thread for the LLM.
        thread_blocks = []
        for t in thread_rows:
            msgs = conn.execute(
                "SELECT from_email, from_name, is_sent, date_received, subject, snippet "
                "FROM email_messages WHERE thread_id=? AND owner_user_id=? "
                "ORDER BY date_received DESC LIMIT 3",
                (t["thread_id"], user_id),
            ).fetchall()
            thread_blocks.append({
                "from": t["from_name"] or t["from_email"],
                "from_email": t["from_email"],
                "subject": t["subject"] or "(no subject)",
                "msg_count": t["msg_count"],
                "recent": list(reversed([dict(m) for m in msgs])),
            })

    stats = {
        "hours": hours,
        "accounts": n_accounts,
        "new_messages": new_count,
        "threads_needing_reply": len(needing_reply),
        "unread_count": unread_count,
    }
    threads_needing_reply_serialised = [{
        "id":         r["id"],                 # integer PK — use for /r/email?msg=
        "message_id": r["message_id"],         # IMAP <…> string — kept for backwards compat
        "thread_id":  r["thread_id"],
        "from":       r["from_name"] or r["from_email"],
        "from_name":  r["from_name"],          # for markdown-link matching (see SmartMarkdown)
        "from_email": r["from_email"],
        "subject":    r["subject"],
    } for r in needing_reply]

    if not thread_blocks:
        return {
            "summary": f"No new email in the last {hours}h. Nothing to act on.",
            "stats": stats,
            "threads_needing_reply": [],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    prompt = _build_prompt(thread_blocks, stats, hours)
    summary = await _call_llm(prompt)

    return {
        "summary": summary,
        "stats": stats,
        "threads_needing_reply": threads_needing_reply_serialised,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _build_prompt(blocks: list[dict], stats: dict, hours: int) -> str:
    lines = [
        f"You are summarising the user's email inbox from the last {hours} hours, "
        f"across {stats['accounts']} account(s).",
        "",
        "Write a tight, scannable briefing in this exact structure:",
        "",
        "**Action items** (the 3-5 most important things to reply to or act on, in priority order)",
        "- Each item: one line, sender name in bold, then what's needed in plain language.",
        "",
        "**By conversation** (one bullet per thread with a one-sentence what-happened)",
        "",
        "Tone: efficient, friendly, no fluff. Write the briefing in the same language as the "
        "majority of the messages — match what the user reads in their inbox.",
        "Don't list every message — synthesise. Newsletters and promotions can be grouped as one bullet.",
        "",
        "── Recent threads ──",
    ]
    for b in blocks:
        lines.append("")
        lines.append(f"## {b['from']} — {b['subject']}  ({b['msg_count']} msg)")
        for m in b["recent"]:
            who = "Me" if m["is_sent"] else (m["from_name"] or m["from_email"])
            lines.append(f"  {who}: {m['snippet'] or '(empty)'}")
    lines.append("")
    lines.append(f"({stats['unread_count']} unread total across all folders.)")
    lines.append("")
    lines.append("Briefing:")
    return "\n".join(lines)
