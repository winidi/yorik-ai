"""email_draft skill — variants for replying to an email thread."""

from __future__ import annotations
import json
import re
from typing import Any, Optional


async def execute(
    ctx,
    message_id: int,
    extra_instructions: Optional[str] = None,
    state: Optional[str] = None,
    variants: int = 3,
) -> dict[str, Any]:
    """Generate reply variants. `state` (one of the STATE_SPECS keys —
    friendly/formal/quick/warm/firm) drives a tone-locked prompt:
    all variants share that tone but vary in angle. Without `state`,
    falls back to the default brief/warm/detailed angle split."""
    if variants not in (1, 3):
        raise ValueError("variants must be 1 or 3")
    user_id = getattr(ctx, "user_id", 1)

    from backend.database import get_conn
    from backend.whatsapp import _call_llm, _calendar_context
    from backend.whatsapp_autodraft import (
        _call_llm_variants,
        VARIANT_SPECS,
        STATE_SPECS,
    )

    with get_conn() as conn:
        # is_sent + date_received included so the standalone-message
        # path (`thread = [msg]` below) can build the same shape the
        # multi-message thread loop expects. Without these two columns
        # the prompt-build at line 83-92 crashes with KeyError: is_sent
        # on any inbound message that has no thread_id (i.e. a fresh
        # cold-inbound, or any message whose Message-ID wasn't recognised
        # for threading).
        msg = conn.execute(
            "SELECT id, thread_id, from_email, from_name, subject, body_text, "
            "       account_id, is_sent, date_received "
            "FROM email_messages WHERE id=? AND owner_user_id=?",
            (message_id, user_id),
        ).fetchone()
        if not msg:
            raise ValueError(f"message not found: {message_id}")
        # Pull whole thread (oldest first) for context. Carry subject +
        # date_received so each message in the prompt has its real
        # email header — short emails ("see attached") often have all
        # the meaning in the subject line, which the previous
        # body-only feed dropped.
        if msg["thread_id"]:
            thread = conn.execute(
                "SELECT from_email, from_name, is_sent, date_received, "
                "       subject, body_text "
                "FROM email_messages WHERE thread_id=? AND owner_user_id=? "
                "ORDER BY date_received ASC LIMIT 20",
                (msg["thread_id"], user_id),
            ).fetchall()
        else:
            thread = [msg]
        # User's first name for the sign-off. Falls back to "" so the
        # prompt's sign-off instruction degrades gracefully (LLM will
        # use whatever it can infer from "how the user writes").
        u = conn.execute(
            "SELECT first_name, name FROM user_profiles WHERE id=?",
            (user_id,),
        ).fetchone()
    user_first_name = ""
    if u:
        user_first_name = (u["first_name"] or "").strip()
        if not user_first_name and (u["name"] or "").strip():
            user_first_name = u["name"].split()[0]

    contact_name = msg["from_name"] or msg["from_email"].split("@")[0]

    # Hits across email + WhatsApp via existing infra.
    last_body = msg["body_text"] or msg["subject"] or ""
    fts_hits   = _email_fts_hits(message_id, user_id, last_body, k=3)
    cross_chat = _wa_cross_hints_for_email(user_id, last_body, k=2)
    sources = fts_hits + cross_chat
    calendar = _calendar_context()

    # Per-message body cap bumped 800 → 3000 chars. WhatsApp messages
    # are short by nature so 800 covers them; emails routinely run
    # multiple paragraphs and the old cap silently chopped the
    # bottom — often the actual ask.
    BODY_CAP = 3000
    email_thread = [
        {
            "from_me":   bool(t["is_sent"]),
            "name":      t["from_name"] or t["from_email"],
            "date":      t["date_received"] or "",
            "subject":   t["subject"] or "",
            "body":      (t["body_text"] or "")[:BODY_CAP],
        }
        for t in thread
    ]

    if variants == 3:
        prompt = _build_email_variants_prompt(
            contact_name=contact_name,
            user_first_name=user_first_name,
            thread=email_thread,
            state_key=state if state and state in STATE_SPECS else None,
            custom_instructions=extra_instructions,
            cross_hits=sources,
            calendar=calendar,
            state_specs=STATE_SPECS,
            variant_specs=VARIANT_SPECS,
        )
        pairs = await _call_llm_variants(prompt)
        drafts = [{"label": label, "text": text} for label, text in pairs]
    else:
        # Single draft — simpler prompt. Same email_thread shape, the
        # single-prompt builder just renders one combined message
        # block instead of three angle-split drafts.
        prompt = _build_single_prompt(contact_name, email_thread, sources, calendar, extra_instructions)
        text = await _call_llm(prompt)
        drafts = [{"label": "default", "text": text}]

    return {"drafts": drafts, "sources": sources}


def _email_fts_hits(current_msg_id: int, user_id: str, query: str, k: int = 3) -> list[dict]:
    """FTS5 across email_messages (excluding the current message)."""
    from backend.database import get_conn
    q = (query or "").strip()
    if len(q) < 4:
        return []
    terms = sorted({w for w in q.split() if len(w) > 4}, key=len, reverse=True)[:3]
    if not terms:
        return []
    match = " OR ".join(terms)
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT m.id, m.from_name, m.from_email, m.subject, m.snippet "
                "FROM email_messages_fts f "
                "JOIN email_messages m ON m.rowid = f.rowid "
                "WHERE f MATCH ? AND m.id != ? AND m.owner_user_id = ? "
                "ORDER BY m.date_received DESC LIMIT ?",
                (match, current_msg_id, user_id, k),
            ).fetchall()
        except Exception:
            return []
    return [{
        "kind": "other_email",
        "ref": str(r["id"]),
        "snippet": f"From email '{r['subject']}' ({r['from_name'] or r['from_email']}): {(r['snippet'] or '')[:140]}",
    } for r in rows]


def _wa_cross_hints_for_email(user_id: str, query: str, k: int = 2) -> list[dict]:
    """FTS5 hits in this user's WhatsApp chats — same person might have
    messaged both ways. Scoped to the requesting user so the email
    draft doesn't pull in someone else's WA history as context."""
    from backend.whatsapp import _cross_chat_hints
    try:
        return _cross_chat_hints("", query, owner_user_id=user_id)[:k]
    except Exception:
        return []


def _build_single_prompt(contact: str, thread: list[dict], sources: list[dict],
                         calendar: list[dict], extra: Optional[str]) -> str:
    from datetime import datetime
    lines = [
        f"You are helping the user draft a reply on email to {contact}.",
        "",
        "Rules:",
        "- Match the language of the most recent inbound message.",
        "- Match the conversation's tone overall.",
        "- Don't repeat the sender's words; respond to them.",
        "- Only state facts that appear below.",
        "- When asked about availability/scheduling, check the calendar section.",
        "- Return ONLY the message text. No greeting line, no signature.",
        "",
        "── Thread ──",
    ]
    for r in thread:
        who = "Me" if r["from_me"] else r["name"]
        subj = r.get("subject") or ""
        date = r.get("date") or ""
        header = f"{who}" + (f" [{date}]" if date else "") + (f" — Subject: {subj!r}" if subj else "")
        lines.append(header + ":")
        lines.append(r.get("body") or "")
    if calendar:
        lines.append("")
        lines.append(f"── Calendar (today: {datetime.now().strftime('%A %Y-%m-%d')}) ──")
        for e in calendar:
            lines.append(f"  {e['date']} {e['time']}: {e['title']}{e['who']}")
    if sources:
        lines.append("")
        lines.append("── Possibly relevant context ──")
        for s in sources:
            lines.append(f"- {s['snippet']}")
    if extra:
        lines.append("")
        lines.append(f"── Extra instructions ──\n{extra}")
    lines.append("")
    lines.append("Draft reply:")
    return "\n".join(lines)


def _build_email_variants_prompt(
    *,
    contact_name: str,
    user_first_name: str,
    thread: list[dict],
    state_key: Optional[str],
    custom_instructions: Optional[str],
    cross_hits: list[dict],
    calendar: Optional[list[dict]],
    state_specs: dict[str, dict[str, str]],
    variant_specs: list,
) -> str:
    """Email-shaped variants prompt. Replaces the WhatsApp-borrowed
    builder so emails get proper structural guidance (paragraphs,
    greeting, sign-off) instead of WA's SHORT-leaning bias.

    When `state_key` is set, all 3 variants share that tone and only
    differ in angle. When None, falls back to the brief/balanced/
    detailed angle split from VARIANT_SPECS — but still email-shaped,
    not WA-shaped.

    Output format MUST be `---DRAFT N---\\n<body>` so _call_llm_variants
    can parse it.
    """
    from datetime import datetime
    your_messages = [r["body"] for r in thread if r["from_me"] and r["body"]]
    is_thread_continuation = any(r["from_me"] for r in thread[:-1])
    sign_off_name = user_first_name or ""

    lines = [
        f"You are helping the user draft an email reply to {contact_name}.",
        "",
        "═══ HARD RULES ═══",
        "- Reply in the language of the most recent inbound email in the thread.",
        "- Real emails have STRUCTURE. Use blank-line separated paragraphs — "
          "don't write one wall of text. Each paragraph addresses ONE point, "
          "typically 1–4 sentences.",
        "- Length should match the email being replied to. Short ask → short "
          "answer. Long detailed email → a fuller reply with several paragraphs.",
        (
            "- This is a MID-THREAD continuation — skip the greeting line, "
            "just write paragraphs."
            if is_thread_continuation else
            "- This is the FIRST reply in the thread — open with a brief "
            "greeting that matches the formality of the incoming email "
            "(e.g. 'Hi <name>,' / 'Hallo <name>,' / 'Sehr geehrte/r <Name>,')."
        ),
        (
            f"- Close with a sign-off appropriate to the tone, then your "
            f"first name on the next line: \"{sign_off_name}\"."
            if sign_off_name else
            "- Close with a sign-off appropriate to the tone (the user's "
            "first name isn't on file — match the style of the user's prior "
            "messages below for the closing)."
        ),
        "- Don't include 'Subject:', 'From:', or 'To:' lines — the frontend "
          "handles email headers separately. Body only.",
        "- Match the user's own writing style with this person — see the "
          "examples below.",
        "- Only state facts that appear in the messages, sources, or "
          "calendar below — don't invent.",
        "- When asked about scheduling, check the Calendar section and "
          "answer concretely (propose specific free slots or name the blocker).",
        "- Each draft must be complete and ready-to-send as-is.",
        "",
    ]

    if state_key and state_key in state_specs:
        spec = state_specs[state_key]
        lines.extend([
            f"═══ REPLY TONE: {spec['label_en']} / {spec['label_de']} ═══",
            spec["tone"],
            "",
            "All 3 drafts share this tone. They DIFFER only in angle / "
            "framing / level of detail — not in tone.",
            "",
        ])
    else:
        # Default brief/balanced/detailed split. VARIANT_SPECS is a list
        # of (label, instruction) pairs.
        lines.append("═══ THREE ANGLES TO COVER ═══")
        for i, (_label, spec) in enumerate(variant_specs, 1):
            lines.append(f"  DRAFT {i}: {spec}")
        lines.append("")

    if your_messages:
        lines.append("═══ HOW THE USER WRITES TO THIS PERSON ═══")
        lines.append("(Most recent emails the user themselves sent — match this style)")
        for body in your_messages[-5:]:
            cleaned = " ".join((body or "").split())[:400]
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

    lines.append("═══ THREAD (oldest first) ═══")
    for r in thread[-15:]:
        who = "Me" if r["from_me"] else (r["name"] or contact_name)
        date_part = f" [{r['date']}]" if r.get("date") else ""
        subj_part = f"  Subject: {r['subject']!r}" if r.get("subject") else ""
        lines.append(f"── {who}{date_part}{subj_part}")
        lines.append(r.get("body") or "[empty body]")
        lines.append("")

    if cross_hits:
        lines.append("═══ RELATED CONTEXT FROM OTHER EMAILS / DOCUMENTS ═══")
        for h in cross_hits[:6]:
            snippet = (h.get("snippet") or h.get("text") or "").replace("\n", " ").strip()[:240]
            if snippet:
                lines.append(f"  • [{h.get('kind', 'ref')}] {snippet}")
        lines.append("")

    if calendar:
        lines.append(f"═══ USER'S UPCOMING CALENDAR (today: {datetime.now().strftime('%A %Y-%m-%d')}) ═══")
        for e in calendar[:10]:
            lines.append(f"  {e.get('date', '')} {e.get('time', '')}: {e.get('title', '')}{e.get('who', '')}")
        lines.append("(Use these ONLY if scheduling is in scope.)")
        lines.append("")

    lines.extend([
        "═══ OUTPUT FORMAT (STRICT) ═══",
        "Three drafts, EXACTLY in this format, nothing else:",
        "---DRAFT 1---",
        "<email body with paragraphs separated by blank lines, greeting if "
        "first contact, sign-off + name>",
        "---DRAFT 2---",
        "<email body>",
        "---DRAFT 3---",
        "<email body>",
        "",
        "Drafts:",
    ])
    return "\n".join(lines)
