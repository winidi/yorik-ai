"""whatsapp_draft skill implementation.

Thin orchestrator on top of the existing whatsapp.py + whatsapp_autodraft.py
machinery. Both the HTTP endpoint (/api/whatsapp/draft) and the
auto-draft background task call into this skill so there is exactly
one place that generates WhatsApp drafts.
"""

from __future__ import annotations

import json
from typing import Any, Optional


async def execute(
    ctx,
    chat_jid: Optional[str] = None,
    contact_id: Optional[int] = None,
    intent: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    variants: int = 1,
    state: Optional[str] = None,
    persist: bool = True,
) -> dict[str, Any]:
    """See skill.md for the contract. Returns
        {drafts: [{label, text}], sources: [...]}

    chat_jid: existing WhatsApp JID (e.g. "491701234567@s.whatsapp.net").
    Optional when contact_id is given — we derive the JID from the
    contact's whatsapp channel value.

    contact_id (new): resolve the JID from contacts. When the contact
    has a WhatsApp channel saved but no prior chat thread exists, the
    skill switches to "initiate" mode (no history, intent-driven
    prompt). Lets users start a brand-new conversation with someone
    they've never messaged on WhatsApp before — previously this path
    hard-failed at "chat not found".

    intent (new): one-line description of what to say, required for
    initiate mode (since there's no thread history for the LLM to
    riff off of). Example: "ask if Friday 3pm at Café Klatsch
    works for coffee".

    state (new): one of friendly | formal | quick | warm | firm. When
    set with variants=3, uses the state-driven prompt builder that
    pins the tone and varies only the angle. When None, falls back to
    the legacy brief/warm/detailed split.

    persist (new): controls whether the generated drafts get written
    to wa_drafts. The user-driven draft-options UI sets persist=False
    so the panel stays ephemeral (no chat-row badges, no DB cruft).
    """
    if variants not in (1, 3):
        raise ValueError("variants must be 1 or 3")

    # Lazy imports — these modules pull heavy deps (httpx, sqlite-vec)
    # and we don't want to pay the import cost at registry-load time.
    from backend import whatsapp as wa
    from backend.database import get_conn

    # SkillContext carries the requesting user's id. Drafts are scoped
    # to whoever asked — wife drafting a reply only sees her own chat
    # data, not admin's.
    user_id = getattr(ctx, "user_id", None) or wa.DEFAULT_OWNER

    # Resolve chat_jid from contact_id when caller didn't pass one.
    # The contact's whatsapp channel value is normalized E.164 (plus
    # sign + digits); WhatsApp JIDs drop the plus and append
    # "@s.whatsapp.net". Same shape the bridge expects on /send.
    contact_obj = None
    if not chat_jid and contact_id:
        from backend import contacts as _contacts_mod
        contact_obj = _contacts_mod.get(int(contact_id))
        if not contact_obj:
            raise ValueError(
                f"contact_id={contact_id} not found. Re-call find_person "
                "or list_contacts_for_picking to resolve a real contact."
            )
        wa_channel = next(
            (ch for ch in (contact_obj.get("channels") or [])
             if ch.get("kind") == "whatsapp" and (ch.get("value") or "").strip()),
            None,
        )
        if not wa_channel:
            raise ValueError(
                f"contact {contact_obj.get('display_name')!r} has no "
                "WhatsApp number on file. Tell the user: 'Für <name> ist "
                "keine WhatsApp-Nummer hinterlegt — soll ich eine "
                "hinzufügen?' Then either add_channel(kind=whatsapp, "
                "value=...) or pick a different recipient."
            )
        import re as _re
        digits = _re.sub(r"\D", "", wa_channel["value"])
        if not digits or len(digits) < 6:
            raise ValueError(
                f"contact {contact_obj.get('display_name')!r}'s WhatsApp "
                f"channel value {wa_channel['value']!r} doesn't look like "
                "a phone number. Ask the user to correct it in contacts."
            )
        chat_jid = f"{digits}@s.whatsapp.net"

    if not chat_jid:
        raise ValueError(
            "whatsapp_draft requires either chat_jid (existing thread) or "
            "contact_id (resolve from contact's WhatsApp channel). Call "
            "find_person first and pass the contact_id."
        )

    with get_conn() as conn:
        chat_row = conn.execute(
            "SELECT jid, name, is_group FROM wa_chats WHERE jid=? AND owner_user_id=?",
            (chat_jid, user_id),
        ).fetchone()
        recent = conn.execute(
            "SELECT from_me, push_name, timestamp, text, transcript "
            "FROM wa_messages WHERE chat_jid=? AND owner_user_id=? "
            "ORDER BY timestamp DESC LIMIT 20",
            (chat_jid, user_id),
        ).fetchall() if chat_row else []

    # Branch: existing thread (has chat_row + messages) vs initiate mode
    # (no thread, model is starting a new conversation). The initiate
    # path needs `intent` since there's no message history to reply to.
    is_initiate = (not chat_row) or (not recent)
    if is_initiate:
        if not intent and not extra_instructions:
            raise ValueError(
                "no prior conversation with this recipient — pass "
                "`intent` (one-line description of what to say) so "
                "the skill knows what to draft. Example: intent='ask "
                "Marie if Friday 3pm coffee at Café Klatsch works'."
            )
        # Friendly contact name for the prompt — prefer contact name,
        # fall back to existing wa_chats.name, finally to the JID's
        # numeric prefix as a last resort.
        contact_name = None
        if contact_obj:
            contact_name = contact_obj.get("display_name")
        if not contact_name and chat_row:
            contact_name = chat_row["name"]
        if not contact_name:
            contact_name = chat_jid.split("@")[0]
        sources = [{"kind": "initiate", "ref": chat_jid,
                     "snippet": "new conversation — no prior thread"}]
        calendar = wa._calendar_context()
        prompt = _build_initiate_prompt(
            contact_name=contact_name,
            intent=(intent or extra_instructions or "").strip(),
            state_key=state,
            extra_instructions=extra_instructions if intent else None,
            calendar=calendar,
            variants=variants,
        )
        if variants == 3:
            from backend import whatsapp_autodraft as _ad
            pairs = await _ad._call_llm_variants(prompt)
            label_base = state or "initiate"
            drafts = [{"label": label_base, "text": text} for _, text in pairs]
        else:
            text = await wa._call_llm(prompt)
            drafts = [{"label": "default", "text": text}]
        import uuid
        group_id = str(uuid.uuid4())
        # Initiate mode CANNOT persist: wa_drafts.chat_jid has a foreign
        # key into wa_chats(jid), and the wa_chats row only gets created
        # when an actual outbound message hits /chats/{jid}/send (which
        # auto-promotes the contact and inserts the seed message + chat
        # row). Auto-creating a wa_chats row here would surface a ghost
        # thread in the inbox before the user has actually committed to
        # sending — the LLM's response already carries the draft text,
        # so the user reads it in chat and either says "send it" (which
        # routes through /send and creates the real row) or refines /
        # discards it.
        return {"drafts": drafts, "sources": sources, "group_id": group_id,
                 "chat_jid": chat_jid, "initiate": True,
                 # Surface "not persisted" so the frontend draft-options
                 # panel doesn't try to fetch the row by group_id.
                 "persisted": False}

    recent = list(reversed(recent))

    last_inbound = next(
        (r["text"] for r in reversed(recent) if not r["from_me"] and r["text"]),
        recent[-1]["text"] or "",
    )

    fts_hits = wa._cross_chat_hints(chat_jid, last_inbound, owner_user_id=user_id)
    sem_hits = wa._semantic_hints(chat_jid, last_inbound)
    pap_hits = wa._paperless_hints(last_inbound)
    cross_hits = wa._merge_hints(fts_hits, sem_hits, pap_hits, cap=6)
    calendar = wa._calendar_context()

    sources = [{"kind": "thread", "ref": chat_jid, "snippet": f"{len(recent)} recent messages"}]
    sources.extend(cross_hits)

    if variants == 3:
        from backend import whatsapp_autodraft as _ad
        if state and state in _ad.STATE_SPECS:
            # State-driven: tone is pinned (friendly/formal/quick/warm/firm),
            # 3 drafts vary only in angle. User's writing style examples
            # are injected from `recent` (from_me messages).
            prompt = _ad._build_state_variants_prompt(
                contact_name=chat_row["name"] or chat_jid.split("@")[0],
                recent=recent,
                state_key=state,
                custom_instructions=extra_instructions,
                cross_hits=cross_hits,
                calendar=calendar,
            )
            label_base = state
        else:
            # Legacy 3-angle split (brief/warm/detailed) — kept for the
            # /draft endpoint and the autodraft path before they migrate.
            prompt = _ad._build_variants_prompt(
                contact_name=chat_row["name"] or chat_jid.split("@")[0],
                recent=recent,
                cross_hits=cross_hits,
                calendar=calendar,
            )
            label_base = None
        pairs = await _ad._call_llm_variants(prompt)
        if label_base:
            # All 3 share the state label — UI doesn't need per-angle
            # naming when the user already picked the tone.
            drafts = [{"label": label_base, "text": text} for _, text in pairs]
        else:
            drafts = [{"label": label, "text": text} for label, text in pairs]
    else:
        prompt = wa._build_draft_prompt(
            contact_name=chat_row["name"] or chat_jid.split("@")[0],
            is_group=bool(chat_row["is_group"]),
            recent=recent,
            cross_hits=cross_hits,
            calendar=calendar,
            extra=extra_instructions,
        )
        text = await wa._call_llm(prompt)
        drafts = [{"label": "default", "text": text}]

    import uuid
    group_id = str(uuid.uuid4())
    if persist:
        # Legacy auto-draft + /draft HTTP endpoint store drafts so the
        # old DraftPanel can render them. The new draft-options UI sets
        # persist=False — drafts are ephemeral, no chat-row badges, no
        # DB cruft.
        with get_conn() as conn:
            for d in drafts:
                conn.execute(
                    "INSERT INTO wa_drafts (chat_jid, draft_text, sources_json, owner_user_id, "
                    "                       status, variant_group_id, variant_label) "
                    "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                    (chat_jid, d["text"], json.dumps(sources), user_id,
                     group_id, d["label"]),
                )
            conn.commit()

    return {"drafts": drafts, "sources": sources, "group_id": group_id}


def _build_initiate_prompt(
    contact_name: str,
    intent: str,
    state_key: Optional[str] = None,
    extra_instructions: Optional[str] = None,
    calendar: Optional[list[dict[str, Any]]] = None,
    variants: int = 1,
) -> str:
    """Prompt for INITIATING a WhatsApp conversation. No thread history;
    the LLM is starting fresh with a stated intent.

    Lives in this skill (not whatsapp_autodraft.py) because it's only
    used from the initiate path here — the autodraft module's other
    builders all consume `recent` and `cross_hits`, which an initiate
    flow doesn't have.
    """
    lines = [
        f"You are helping the user start a NEW WhatsApp chat with {contact_name}.",
        "There is NO prior thread history — this is the first message you're sending to this person on WhatsApp.",
        "",
        "═══ HARD RULES ═══",
        "- WhatsApp messages are SHORT. 1–3 sentences is typical.",
        "- Open with a brief friendly hello (Hi/Hallo/Hey + name) since this is your first message.",
        "- Match the language of the INTENT below — German intent → German message, English intent → English message.",
        "- Be direct and warm; this is messaging, not a formal letter.",
        "- Don't fabricate shared history — keep it grounded in the intent.",
        "- Only state facts that appear in the intent (or the user's notes / calendar below).",
        "",
        "═══ INTENT (what to say) ═══",
        intent.strip(),
        "",
    ]
    if state_key:
        # Reuse STATE_SPECS from whatsapp_autodraft for tone consistency.
        try:
            from backend.whatsapp_autodraft import STATE_SPECS as _SS
            spec = _SS.get(state_key, _SS.get("friendly"))
            if spec:
                lines.extend([
                    f"═══ TONE: {spec.get('label_en', state_key)} ═══",
                    spec.get("tone", ""),
                    "",
                ])
        except Exception:
            pass
    if extra_instructions and extra_instructions.strip():
        lines.extend([
            "═══ USER'S ADDITIONAL NOTES ═══",
            extra_instructions.strip(),
            "",
        ])
    if calendar:
        lines.append("═══ USER'S UPCOMING CALENDAR (next 7 days, for scheduling context) ═══")
        for e in calendar[:10]:
            when = e.get("when", "")
            title = (e.get("title", "") or "").strip()
            lines.append(f"  • {when} — {title}")
        lines.append("")
    if variants == 3:
        lines.extend([
            "═══ TASK ═══",
            "Produce exactly 3 short opener drafts. All 3 must:",
            "  • Match the tone above.",
            "  • Be SHORT (typical WhatsApp length).",
            "Differentiate the 3 by ANGLE (direct vs softer vs question-led), not by tone.",
            "",
            "Format STRICTLY like this — three blocks, nothing else, no commentary:",
            "---DRAFT 1---",
            "<text>",
            "---DRAFT 2---",
            "<text>",
            "---DRAFT 3---",
            "<text>",
        ])
    else:
        lines.extend([
            "═══ TASK ═══",
            "Produce ONE short WhatsApp opener ready to send. Return ONLY the message text — no headings, no labels, no commentary.",
        ])
    return "\n".join(lines)
