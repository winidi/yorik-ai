"""Contact retriever — the identity anchor every prompt needs.

Returns a single Evidence row with the contact's display_name, kind,
relation, and any free-form notes. The LLM uses this to know who
it's writing about ("Anna is the user's mother" — drives tone).
"""

from __future__ import annotations

from ..registry import ContextRetriever, Evidence, RetrieverContext, register_retriever


async def _fetch(ctx: RetrieverContext) -> list[Evidence]:
    if ctx.contact_id is None:
        return []
    from ...database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, display_name, kind, relation, notes FROM contacts WHERE id=?",
            (ctx.contact_id,),
        ).fetchone()
    if not row:
        return []
    bits = [row["display_name"] or ""]
    if row["relation"]:
        bits.append(f"({row['relation']})")
    if row["kind"] == "business":
        bits.append("[business]")
    if row["notes"]:
        bits.append(f"— {row['notes']}"[:100])
    snippet = " ".join(b for b in bits if b)
    return [Evidence(kind="contact", ref_id=int(row["id"]), snippet=snippet)]


register_retriever(ContextRetriever(
    name="contact",
    scope=["message", "contact"],
    fetch=_fetch,
    user_disable_ok=False,  # always-on; the LLM needs the identity
))
