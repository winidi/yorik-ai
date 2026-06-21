"""Email-history retriever — last 5 emails to/from the contact.

Gives the LLM threading + relationship context: are we in the middle
of a back-and-forth? Did the user already reply to a previous message
in the thread? What tone has the conversation taken?

Excludes the current message itself so the LLM doesn't double-count it.
"""

from __future__ import annotations

from ..registry import ContextRetriever, Evidence, RetrieverContext, register_retriever


async def _fetch(ctx: RetrieverContext) -> list[Evidence]:
    if ctx.contact_id is None:
        return []
    from ...database import get_conn
    with get_conn() as conn:
        # Channel emails for this contact — there can be multiple
        # (work + personal). Match any of them.
        ch_rows = conn.execute(
            "SELECT value FROM contact_channels "
            "WHERE contact_id=? AND kind='email'",
            (ctx.contact_id,),
        ).fetchall()
    emails = [(r["value"] or "").lower() for r in ch_rows if r["value"]]
    if not emails:
        return []
    placeholders = ",".join(["?"] * len(emails))
    # Build a "from this contact OR to this contact" filter. to_addrs
    # is TEXT-encoded JSON in the schema — substring match is a
    # cheap-and-correct enough proxy.
    or_to = " OR ".join(["LOWER(to_addrs) LIKE ?"] * len(emails))
    to_patterns = [f"%{e}%" for e in emails]
    current_id = int(ctx.source_id) if ctx.source_kind == "email" else -1

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, subject, snippet, date_received, is_sent "
            f"FROM email_messages "
            f"WHERE id != ? AND ("
            f"  LOWER(from_email) IN ({placeholders}) "
            f"  OR ({or_to})"
            f") "
            f"ORDER BY date_received DESC NULLS LAST LIMIT 5",
            (current_id, *emails, *to_patterns),
        ).fetchall()

    out: list[Evidence] = []
    for r in rows:
        direction = "→ you" if not r["is_sent"] else "you →"
        date = (r["date_received"] or "")[:10]
        subj = (r["subject"] or "(no subject)")[:80]
        out.append(Evidence(
            kind="email_message",
            ref_id=int(r["id"]),
            snippet=f"{direction} {date}: {subj}"[:140],
        ))
    return out


register_retriever(ContextRetriever(
    name="email_history",
    scope=["message"],
    fetch=_fetch,
))
