"""Auto-draft replies on incoming WhatsApp messages.

When a new inbound 1:1 text message lands, Yorik debounces 3 seconds
(so a burst of messages collapses into a single draft pass) and then
generates 3 distinct draft variants — brief, warm, detailed — using
the same draft pipeline as manual `/draft` (cross-chat semantic + FTS5
+ Paperless retrieval), but with a single LLM call that returns all 3
in one response to avoid 3× round-trips.

Variants are stored in wa_drafts with a shared variant_group_id and
status='pending'. They surface in the UI's draft panel the moment the
user opens the chat. The user can:
  - Click a variant to load into the send field
  - Regenerate (kills the current set, makes a new one)
  - Discard the set

They're also automatically discarded if the user replies manually
from their phone — we see the outgoing fromMe message via Baileys'
linked-device feed and mark the chat's pending drafts as
status='discarded', reason='manual_reply'. No stale drafts pollute
the panel when you've already handled the chat elsewhere.

Skip rules (no auto-draft if):
  - From self (fromMe) — that's our own send
  - Group chat — too noisy + groups expect human-in-the-loop
  - No text (media-only) — nothing to draft from
  - < 12 chars (ack messages like "ok", "thumbs", "lol")
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Optional

from .database import get_conn

log = logging.getLogger("yorik.whatsapp.autodraft")

# How long to wait after the latest message before drafting. If another
# message arrives in this window we cancel + reschedule — bursts
# collapse to a single draft. Long enough that a quick 2-message reply
# doesn't generate two draft sets; short enough that the user perceives
# drafts as "ready when I open the chat."
DEBOUNCE_S = 3.0

# Per-chat scheduled draft tasks. Cancelling a pending task replaces it
# with a fresh one when a newer message arrives.
_pending: dict[str, asyncio.Task] = {}

# Per-chat lock so we never run two draft generations concurrently for
# the same chat (race between debounced trigger and manual /draft).
_locks: dict[str, asyncio.Lock] = {}


def _should_autodraft(msg: dict[str, Any]) -> bool:
    if msg.get("fromMe"):
        return False
    jid = msg.get("jid") or ""
    if jid.endswith("@g.us"):
        return False  # group — too noisy
    text = (msg.get("text") or "").strip()
    if len(text) < 12:
        return False
    return True


def schedule(msg: dict[str, Any], owner_user_id: str = 1) -> None:
    """Called from the WS event handler for every ingested message.
    Debounces + spawns a draft task. Fire-and-forget.

    owner_user_id is the Yorik user who owns the WhatsApp session this
    message came in on — auto-drafts will be stored and surfaced to
    that user only. Default of 1 keeps legacy single-tenant callers
    working unchanged."""
    if not _should_autodraft(msg):
        return
    jid = msg["jid"]
    msg_id = msg["id"]
    # Debounce key includes owner so two users in the same group chat
    # don't cancel each other's pending drafts.
    key = (owner_user_id, jid)
    prev = _pending.get(key)
    if prev and not prev.done():
        prev.cancel()
    _pending[key] = asyncio.create_task(
        _debounced_draft(jid, msg_id, owner_user_id),
        name=f"autodraft:{owner_user_id}:{jid}",
    )


async def _debounced_draft(chat_jid: str, trigger_msg_id: str, owner_user_id: str = 1) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_S)
    except asyncio.CancelledError:
        return  # newer message landed, our slot was replaced

    # Per-owner lock so two users in the same group don't serialise.
    lock_key = (owner_user_id, chat_jid)
    lock = _locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        try:
            await _generate_and_store(chat_jid, trigger_msg_id, owner_user_id)
        except Exception as e:
            log.exception("autodraft for user=%s %s failed: %s", owner_user_id, chat_jid, e)


async def _generate_and_store(chat_jid: str, trigger_msg_id: str, owner_user_id: str = 1) -> None:
    # Pull thread context the same way the manual draft endpoint does.
    # Lazy import — avoids circular dependency at module load.
    from . import whatsapp as wa

    with get_conn() as conn:
        chat_row = conn.execute(
            "SELECT jid, name, is_group FROM wa_chats WHERE jid=? AND owner_user_id=?",
            (chat_jid, owner_user_id),
        ).fetchone()
        if not chat_row:
            return
        recent = conn.execute(
            "SELECT from_me, push_name, timestamp, text, transcript "
            "FROM wa_messages WHERE chat_jid=? AND owner_user_id=? "
            "ORDER BY timestamp DESC LIMIT 20",
            (chat_jid, owner_user_id),
        ).fetchall()
    if not recent:
        return
    recent = list(reversed(recent))
    last_inbound = next((r["text"] for r in reversed(recent) if not r["from_me"] and r["text"]), recent[-1]["text"] or "")

    # Retrieve same three channels as manual draft + upcoming calendar.
    # FTS hints are owner-scoped so we don't leak admin's history into
    # wife's drafts. Semantic + Paperless hits remain shared (they're
    # about cross-modal context, not chat data).
    fts_hits = wa._cross_chat_hints(chat_jid, last_inbound, owner_user_id=owner_user_id)
    sem_hits = wa._semantic_hints(chat_jid, last_inbound)
    pap_hits = wa._paperless_hints(last_inbound)
    sources = wa._merge_hints(fts_hits, sem_hits, pap_hits, cap=6)
    calendar = wa._calendar_context()

    prompt = _build_variants_prompt(
        contact_name=chat_row["name"] or chat_jid.split("@")[0],
        recent=recent,
        cross_hits=sources,
        calendar=calendar,
    )

    variants = await _call_llm_variants(prompt)
    if not variants:
        return

    group_id = str(uuid.uuid4())
    # First, mark any older pending drafts for THIS user + chat as
    # superseded — only one pending set per (owner, chat).
    with get_conn() as conn:
        conn.execute(
            "UPDATE wa_drafts SET status='discarded', discarded_at=datetime('now'), "
            "discard_reason='regenerated' WHERE chat_jid=? AND owner_user_id=? AND status='pending'",
            (chat_jid, owner_user_id),
        )
        for label, text in variants:
            conn.execute(
                "INSERT INTO wa_drafts (chat_jid, draft_text, sources_json, owner_user_id, "
                "                       status, variant_group_id, variant_label, trigger_msg_id) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                (chat_jid, text, json.dumps(sources), owner_user_id,
                 group_id, label, trigger_msg_id),
            )
        conn.commit()

    # Push to THIS user's open browser tabs — UI refreshes draft panel.
    try:
        await wa._broadcast_to_browsers({
            "type": "drafts_updated",
            "payload": {"chat_jid": chat_jid, "group_id": group_id, "count": len(variants)},
        }, user_id=owner_user_id)
    except Exception:
        pass
    log.info("auto-drafted %d variants for user=%s %s (group=%s)",
             len(variants), owner_user_id, chat_jid, group_id[:8])


# ───────────────────────── prompt + parsing ────────────────────────────

_VARIANT_RE = re.compile(r"---DRAFT (\d+)---\s*\n?(.*?)(?=---DRAFT \d+---|$)", re.DOTALL)

VARIANT_SPECS = [
    ("brief",    "Brief and direct — the minimum words needed to answer. No greeting if mid-thread."),
    ("warm",     "Warm and personal — friendly tone, conversational. Use light personality but stay concise."),
    ("detailed", "Detailed — fuller response covering all relevant points. Still under 4 sentences."),
]


# ─── User-driven draft states ─────────────────────────────────────────
#
# The user picks one of 5 emotional states; we generate 3 angles in
# that state. Tone instructions are intentionally specific so the LLM
# doesn't blur the states together — every state ALSO inherits the
# "short like real WhatsApp messages" + "match the user's writing style
# with this person" rules in the prompt body.

STATE_SPECS: dict[str, dict[str, str]] = {
    "friendly": {
        "label_en": "Friendly", "label_de": "Freundlich",
        "tone": ("Warm, casual, friendly — like talking to a friend. Use the user's natural "
                 "informal style (matches the examples below). Light personality OK; emoji OK if "
                 "the user has used them before in this chat, otherwise skip."),
    },
    "formal": {
        "label_en": "Formal", "label_de": "Förmlich",
        "tone": ("Polite, professional, business-like. For German contacts default to 'Sie' form "
                 "unless the prior messages clearly use 'du'. No emoji. Mid-thread: no greeting; "
                 "first contact: open with a brief polite greeting."),
    },
    "quick": {
        "label_en": "Quick", "label_de": "Kurz",
        "tone": ("Extremely brief — one short sentence, or even just a few words / single emoji. "
                 "No greetings, no closings, no fluff. Think 'Ok, klingt gut' / 'sure thing' "
                 "energy."),
    },
    "warm": {
        "label_en": "Caring", "label_de": "Warm",
        "tone": ("Empathetic, supportive, caring. Acknowledge the other person's feelings before "
                 "answering. For difficult topics (condolences, hard news, conflict): gentle, "
                 "validating, no platitudes. Keep it human, not therapy-speak."),
    },
    "firm": {
        "label_en": "Firm", "label_de": "Bestimmt",
        "tone": ("Polite but firm — set a boundary, decline, or push back without being rude. "
                 "Clear what the user is saying no to OR what they're holding to, no over-"
                 "apologising, no waffling. Keep the door open if appropriate but don't soften "
                 "the actual answer."),
    },
}


def _build_state_variants_prompt(
    contact_name: str,
    recent: list[Any],
    state_key: str,
    custom_instructions: Optional[str],
    cross_hits: list[dict[str, Any]],
    calendar: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Variant of _build_variants_prompt that takes a user-chosen tone
    (state_key) + optional custom one-liner instead of the fixed
    brief/warm/detailed angle split. All 3 produced drafts SHARE the
    state's tone but differ in angle."""
    from datetime import datetime
    spec = STATE_SPECS.get(state_key, STATE_SPECS["friendly"])
    your_messages = [r["text"] for r in recent if r["from_me"] and r["text"]]

    lines = [
        f"You are helping the user draft a WhatsApp reply to {contact_name}.",
        "",
        "═══ HARD RULES ═══",
        "- Match the language of the most recent incoming message.",
        "- WhatsApp messages are SHORT. Most real messages are 1–2 sentences. Don't write essays.",
        "- Match the user's own writing style with this specific person (see examples below).",
        "- Don't add greetings ('Hi X!') mid-thread.",
        "- Only state facts that appear in the messages or sources below — don't invent.",
        "- When asked about scheduling, check the Calendar section and answer concretely.",
        "- Each draft must be complete and ready-to-send as-is.",
        "",
        f"═══ REPLY TONE: {spec['label_en']} / {spec['label_de']} ═══",
        spec["tone"],
        "",
    ]

    if your_messages:
        lines.append("═══ HOW THE USER WRITES TO THIS PERSON ═══")
        lines.append("(Last messages the user themselves sent in this chat — match this style)")
        for msg in your_messages[-10:]:
            cleaned = msg.replace("\n", " ").strip()
            if cleaned:
                lines.append(f"  • {cleaned}")
        lines.append("")

    if custom_instructions and custom_instructions.strip():
        lines.extend([
            "═══ USER'S SPECIFIC NUDGE FOR THIS REPLY ═══",
            custom_instructions.strip(),
            "(Let this shape the CONTENT; the tone above still applies.)",
            "",
        ])

    lines.append("═══ RECENT CONVERSATION (oldest first) ═══")
    for r in recent[-15:]:
        speaker = "USER" if r["from_me"] else "THEM"
        text = (r["text"] or "[media]").replace("\n", " ").strip()
        lines.append(f"  {speaker}: {text}")
    lines.append("")

    if cross_hits:
        lines.append("═══ RELATED HISTORY FROM OTHER CHATS / DOCUMENTS ═══")
        for h in cross_hits[:6]:
            snippet = (h.get("snippet") or h.get("text") or "").replace("\n", " ").strip()[:200]
            if snippet:
                lines.append(f"  • [{h.get('kind', 'ref')}] {snippet}")
        lines.append("")

    if calendar:
        lines.append("═══ USER'S UPCOMING CALENDAR (next 7 days) ═══")
        for e in calendar[:10]:
            when = e.get("when", "")
            title = (e.get("title", "") or "").strip()
            lines.append(f"  • {when} — {title}")
        lines.append("")

    lines.extend([
        "═══ TASK ═══",
        "Produce exactly 3 drafts replying to the most recent THEM message. All 3 must:",
        f"  • Carry the {spec['label_en']} tone described above.",
        "  • Match the user's natural writing style.",
        "  • Be SHORT (typical WhatsApp length).",
        "Differentiate the 3 by ANGLE (e.g. agree vs ask back vs propose; or yes vs maybe vs decline),",
        "not by tone — tone is fixed.",
        "",
        "Format STRICTLY like this — three blocks, nothing else, no commentary:",
        "---DRAFT 1---",
        "<text>",
        "---DRAFT 2---",
        "<text>",
        "---DRAFT 3---",
        "<text>",
    ])
    return "\n".join(lines)


def _build_variants_prompt(contact_name: str, recent: list[Any],
                            cross_hits: list[dict[str, Any]],
                            calendar: Optional[list[dict[str, Any]]] = None) -> str:
    from datetime import datetime
    lines = [
        f"You are helping the user draft a WhatsApp reply to {contact_name}.",
        "",
        "Hard rules:",
        "- Match the language of the last incoming message.",
        "- Match the conversation's tone overall.",
        "- Don't add greetings ('Hi X!') mid-thread.",
        "- Only state facts that appear in the messages or sources below.",
        "- When asked about availability/scheduling, check the calendar section "
          "and answer concretely (propose specific free slots or name the blocker).",
        "- Each draft must be a complete, send-ready message.",
        "",
        "Produce exactly 3 drafts, each a DIFFERENT angle:",
    ]
    for i, (_label, spec) in enumerate(VARIANT_SPECS, 1):
        lines.append(f"  DRAFT {i}: {spec}")
    lines.append("")
    lines.append("Format STRICTLY like this — three blocks, nothing else:")
    lines.append("---DRAFT 1---")
    lines.append("<text>")
    lines.append("---DRAFT 2---")
    lines.append("<text>")
    lines.append("---DRAFT 3---")
    lines.append("<text>")
    lines.append("")
    lines.append("── Conversation so far ──")
    for r in recent:
        who = "Me" if r["from_me"] else (r["push_name"] or contact_name)
        text = r["text"] or r["transcript"] or "[media message]"
        lines.append(f"{who}: {text}")
    if calendar:
        lines.append("")
        lines.append(f"── Your upcoming calendar (today is {datetime.now().strftime('%A %Y-%m-%d')}) ──")
        for e in calendar:
            lines.append(f"  {e['date']} {e['time']}: {e['title']}{e['who']}")
        lines.append("(Use these ONLY if scheduling is in scope.)")
    if cross_hits:
        lines.append("")
        lines.append("── Possibly relevant context ──")
        for h in cross_hits:
            lines.append(f"- {h['snippet']}")
    lines.append("")
    lines.append("Drafts:")
    return "\n".join(lines)


async def _call_llm_variants(prompt: str) -> list[tuple[str, str]]:
    """Single LLM call returning 3 variants. Parse the labelled blocks
    and return [(label, text), ...] in spec order."""
    from . import whatsapp as wa

    raw = await wa._call_llm(prompt)
    if not raw:
        return []

    out: list[tuple[str, str]] = []
    for m in _VARIANT_RE.finditer(raw):
        idx = int(m.group(1)) - 1
        if idx < 0 or idx >= len(VARIANT_SPECS):
            continue
        text = m.group(2).strip()
        # Strip stray quote-wrapping the LLM sometimes adds.
        if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
            text = text[1:-1].strip()
        if text:
            out.append((VARIANT_SPECS[idx][0], text))

    # If the LLM ignored the format, salvage by splitting on blank
    # lines and taking the first 3 non-empty paragraphs.
    if not out:
        chunks = [c.strip() for c in re.split(r"\n\s*\n", raw) if c.strip()]
        for i, c in enumerate(chunks[:3]):
            out.append((VARIANT_SPECS[i][0], c))

    return out[:3]


# ───────────────────────── discard on manual reply ─────────────────────

def discard_on_manual_reply(msg: dict[str, Any], owner_user_id: str = 1) -> int:
    """Called when a fromMe message lands on owner_user_id's session.
    If the fromMe message wasn't sent through Yorik (no draft has this
    msg_id as sent_msg_id for this user), mark all pending drafts in
    that chat as discarded — the user has already replied manually
    from their phone. Scoped to a single user so admin's reply doesn't
    blast away wife's pending drafts."""
    if not msg.get("fromMe"):
        return 0
    chat_jid = msg.get("jid")
    msg_id = msg.get("id")
    if not chat_jid or not msg_id:
        return 0

    with get_conn() as conn:
        # Was this our own send (Yorik used a draft to send it)?
        ours = conn.execute(
            "SELECT 1 FROM wa_drafts WHERE chat_jid=? AND sent_msg_id=? AND owner_user_id=?",
            (chat_jid, msg_id, owner_user_id),
        ).fetchone()
        if ours:
            return 0  # we did this send, don't kill the bookkeeping
        cur = conn.execute(
            "UPDATE wa_drafts SET status='discarded', discarded_at=datetime('now'), "
            "discard_reason='manual_reply' WHERE chat_jid=? AND owner_user_id=? AND status='pending'",
            (chat_jid, owner_user_id),
        )
        conn.commit()
        return cur.rowcount or 0
