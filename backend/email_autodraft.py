"""Email auto-draft scheduler.

When a new inbound email is ingested by the fetcher, schedule_for_message
queues a debounced LLM call. After AUTODRAFT_DEBOUNCE_S of quiet, the
email_draft skill runs (3 variants in one LLM call) and the results are
persisted in email_drafts with status='pending'.

The UI's Reader pane queries pending drafts for the visible message
and renders them under the body. User picks one → sent → marked 'used',
siblings 'discarded'.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional

from .database import get_conn

log = logging.getLogger("yorik.email.autodraft")

DEBOUNCE_S = 5.0

# message_id → asyncio.Task. New schedule for the same message cancels
# the previous (debounce).
_pending: dict[int, asyncio.Task] = {}


def schedule_for_message(owner_user_id: str, message_id: int, account_id: int) -> None:
    """Called from the IMAP fetcher (in a worker thread) when a new
    inbound message lands. We schedule onto the main event loop via
    asyncio.run_coroutine_threadsafe — the supervisor task is its
    holder."""
    try:
        # Try the current event loop first (works when called from
        # the main thread / async context). asyncio.get_running_loop
        # raises if not in a running loop, which is fine.
        loop = asyncio.get_event_loop()
        if not loop.is_running():
            return
    except RuntimeError:
        return

    # Cancel any existing pending draft for this message.
    prev = _pending.get(message_id)
    if prev and not prev.done():
        prev.cancel()

    async def _coro():
        try:
            await asyncio.sleep(DEBOUNCE_S)
            await _generate_and_store(owner_user_id, message_id)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning("email autodraft for msg %d failed: %s", message_id, e)

    try:
        # Schedule from any thread back to the main loop.
        fut = asyncio.run_coroutine_threadsafe(_coro(), loop)
        # Store as a Task-ish — we don't actually need to await it,
        # just need a handle to cancel.
        _pending[message_id] = fut  # type: ignore[assignment]
    except Exception as e:
        log.debug("schedule failed: %s", e)


async def _generate_and_store(owner_user_id: str, message_id: int,
                                extra_instructions: Optional[str] = None,
                                state: Optional[str] = None) -> None:
    """Generate + persist 3 draft variants for a message.

    `extra_instructions`: user's free-text intent for the reply.
    `state`: one of STATE_SPECS keys (friendly/formal/quick/warm/firm)
      — when set, all 3 variants share that tone and only vary in
      angle. Mirrors the WhatsApp DraftPanel UX.
    """
    from .skills import get_registry, SkillContext

    reg = get_registry()
    skill = reg.get("email_draft")
    if not skill:
        return

    ctx = SkillContext(reg, role="admin", user_id=owner_user_id)
    try:
        kwargs: dict = {"message_id": message_id, "variants": 3}
        if extra_instructions:
            kwargs["extra_instructions"] = extra_instructions
        if state:
            kwargs["state"] = state
        result = await reg.invoke("email_draft", ctx=ctx, **kwargs)
    except Exception as e:
        log.warning("email_draft skill failed for msg %d: %s", message_id, e)
        return

    drafts = result.get("drafts", [])
    sources = result.get("sources", [])
    if not drafts:
        return

    # Get the thread_id for the message so we can also mark thread-level
    # drafts as superseded.
    with get_conn() as conn:
        row = conn.execute(
            "SELECT thread_id FROM email_messages WHERE id=?",
            (message_id,),
        ).fetchone()
    thread_id = row["thread_id"] if row else None

    group_id = str(uuid.uuid4())
    with get_conn() as conn:
        # Discard previous pending drafts for this thread (newer drafts
        # supersede). Same pattern as whatsapp_autodraft.
        if thread_id:
            conn.execute(
                "UPDATE email_drafts SET status='discarded', discarded_at=datetime('now'), "
                "discard_reason='regenerated' WHERE thread_id=? AND status='pending'",
                (thread_id,),
            )
        for d in drafts:
            conn.execute(
                "INSERT INTO email_drafts (message_id, thread_id, draft_text, variant_label, "
                "                          variant_group_id, sources_json, owner_user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, thread_id, d["text"], d.get("label"), group_id,
                 json.dumps(sources), owner_user_id),
            )
        conn.commit()
    log.info("auto-drafted %d variants for email msg %d", len(drafts), message_id)


_DOC_URL_RES = (
    # Paperless mirror — preview / download / raw endpoints all carry the id
    (r"^/paperless/api/documents/(?P<id>\d+)/", "paperless"),
    # Yorik's own /api/documents/<id>/raw for locally uploaded docs
    (r"^/api/documents/(?P<id>-?\d+)/raw", "local"),
)


def _document_snippet_from_url(url: str, max_chars: int = 400) -> str:
    """Return a short text snippet from the indexed document the URL
    refers to, or "" when we can't resolve / fetch it.

    Used by generate_compose_drafts to ground PDF/document attachments
    in real content — so the LLM can say "prices start at €399" from
    the actual catalogue rather than hallucinating. Image-MIME
    attachments don't go through here.
    """
    import re as _re
    import sqlite3
    from .database import DEFAULT_DOCS_DB_PATH

    src: Optional[str] = None
    doc_id: Optional[int] = None
    for pattern, kind in _DOC_URL_RES:
        m = _re.search(pattern, url)
        if m:
            try:
                doc_id = int(m.group("id"))
            except (ValueError, KeyError):
                continue
            src = kind
            break
    if doc_id is None or src is None:
        return ""

    try:
        with sqlite3.connect(DEFAULT_DOCS_DB_PATH, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            if src == "paperless":
                rows = conn.execute(
                    "SELECT text FROM paperless_chunks "
                    "WHERE paperless_doc_id=? ORDER BY chunk_index LIMIT 2",
                    (doc_id,),
                ).fetchall()
            else:  # local doc
                # local doc_ids may be negative (Paperless-source mirror
                # row); abs() to handle either.
                rows = conn.execute(
                    "SELECT text FROM document_chunks "
                    "WHERE doc_id=? ORDER BY chunk_index LIMIT 2",
                    (abs(doc_id),),
                ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("document snippet lookup failed for %s: %s", url, exc)
        return ""

    if not rows:
        return ""
    text = " ".join((r["text"] or "").strip() for r in rows)
    text = " ".join(text.split())  # collapse whitespace
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return text


async def generate_compose_drafts(
    owner_user_id: str,
    *,
    intent: str,
    to_address: Optional[str] = None,
    subject: Optional[str] = None,
    state: Optional[str] = None,
    attachments: Optional[list[dict]] = None,
) -> dict:
    """Three draft variants for a NEW email (not a reply).

    No persistence — these are returned inline and held in the
    Composer's React state. The user picks one, it goes into the body
    editor, and the rest are discarded when the modal closes.

    `intent` is the user's free-text brief ("decline politely, say I'm
    on holiday until next week"). `state` (friendly / formal / quick /
    warm / firm) locks the tone; all three variants share it and only
    differ in angle.

    `attachments` (optional) is a list of {filename, mimetype} dicts
    so the prompt can reference them ("see the attached price list"
    etc.) — the LLM uses the names to decide how to mention each one.

    Returns {variants: [{label, text}, ...], suggested_subject: str}.
    The Composer fills the Subject field with `suggested_subject`
    only when the user hasn't typed one already.
    """
    import re as _re
    from .whatsapp_autodraft import _VARIANT_RE, STATE_SPECS, VARIANT_SPECS
    from .whatsapp import _call_llm

    # User's first name for the sign-off line — same lookup as the
    # reply path so the new-email drafts close consistently.
    with get_conn() as conn:
        u = conn.execute(
            "SELECT first_name, name, language FROM user_profiles WHERE id=?",
            (owner_user_id,),
        ).fetchone()
    user_first_name = ""
    user_language = "en"
    if u:
        user_first_name = (u["first_name"] or "").strip()
        if not user_first_name and (u["name"] or "").strip():
            user_first_name = u["name"].split()[0]
        # Profile language is the source of truth. The user explicitly
        # picked it in Settings — honor it for every draft, even when
        # their intent text is in a different language. Falls back to
        # English when no setting / unknown code.
        user_language = (u["language"] or "en").strip().lower() or "en"

    # Pull the most recent N emails the user themselves sent to this
    # recipient — gives the LLM a style reference so the new drafts
    # sound like the user, not generic LLM prose. Empty list when the
    # recipient is brand-new.
    style_examples: list[str] = []
    if to_address:
        addr = to_address.strip().lower()
        if addr:
            with get_conn() as conn:
                rows = conn.execute(
                    "SELECT body_text FROM email_messages "
                    "WHERE owner_user_id=? AND is_sent=1 "
                    "  AND LOWER(to_addrs) LIKE ? "
                    "ORDER BY date_sent DESC LIMIT 5",
                    (owner_user_id, f"%{addr}%"),
                ).fetchall()
            for r in rows:
                cleaned = " ".join((r["body_text"] or "").split())[:400]
                if cleaned:
                    style_examples.append(cleaned)

    # Hydrate attachment dicts with a small content snippet for
    # documents. Images stay snippet-less — there's no text to pull
    # and the LLM must NOT invent narrative for them (see the HARD
    # RULES in the prompt builder).
    hydrated_atts: list[dict] = []
    for a in (attachments or []):
        d = dict(a)
        url = (d.get("source_url") or "").strip()
        if url and (d.get("mimetype") or "").startswith(("application/", "text/")):
            snip = _document_snippet_from_url(url)
            if snip:
                d["snippet"] = snip
        hydrated_atts.append(d)

    prompt = _build_compose_variants_prompt(
        intent=intent,
        to_address=to_address or "",
        subject=subject or "",
        user_first_name=user_first_name,
        user_language=user_language,
        style_examples=style_examples,
        state_key=state if state and state in STATE_SPECS else None,
        state_specs=STATE_SPECS,
        variant_specs=VARIANT_SPECS,
        attachments=hydrated_atts,
        request_subject=not (subject or "").strip(),
    )
    raw = await _call_llm(prompt) or ""

    # Parse: optional "SUBJECT: …" line at the top, then the standard
    # ---DRAFT N--- blocks the email_draft reply path already produces.
    suggested_subject = ""
    body_part = raw
    sm = _re.match(r"^\s*SUBJECT\s*:\s*(.+?)\s*\n", raw, flags=_re.IGNORECASE)
    if sm:
        suggested_subject = sm.group(1).strip().strip('"').strip("'")
        body_part = raw[sm.end():]

    pairs: list[tuple[str, str]] = []
    for m in _VARIANT_RE.finditer(body_part):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(VARIANT_SPECS):
            text = m.group(2).strip()
            if (text.startswith('"') and text.endswith('"')) or (
                    text.startswith("'") and text.endswith("'")):
                text = text[1:-1].strip()
            if text:
                pairs.append((VARIANT_SPECS[idx][0], text))
    # Salvage fallback for malformed output — same shape as
    # _call_llm_variants does in whatsapp_autodraft.
    if not pairs:
        chunks = [c.strip() for c in _re.split(r"\n\s*\n", body_part) if c.strip()]
        for i, c in enumerate(chunks[:3]):
            pairs.append((VARIANT_SPECS[i][0], c))

    return {
        "variants": [{"label": label, "text": text} for label, text in pairs[:3]],
        "suggested_subject": suggested_subject,
    }


_LANG_NAMES_FOR_PROMPT = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pl": "Polish",
    "nl": "Dutch",
    "pt": "Portuguese",
}


def _build_compose_variants_prompt(
    *,
    intent: str,
    to_address: str,
    subject: str,
    user_first_name: str,
    user_language: str,
    style_examples: list[str],
    state_key: Optional[str],
    state_specs: dict,
    variant_specs: list,
    attachments: list[dict],
    request_subject: bool,
) -> str:
    """Prompt for generating 3 variants of a NEW (non-reply) email.
    Mirrors `_build_email_variants_prompt` in skills/email_draft/skill.py
    but framed for compose instead of reply — no thread to ground on,
    so the user's intent is the entire brief."""
    recipient = (to_address or "the recipient").strip() or "the recipient"
    sign_off_name = user_first_name or ""
    lines = [
        f"You are helping the user compose a NEW email to {recipient}.",
        f"The user has summarised what they want to say below — turn that "
        f"into a well-structured, ready-to-send email body.",
        "",
        "═══ HARD RULES ═══",
        f"- Write the email and the subject line in **{_LANG_NAMES_FOR_PROMPT.get(user_language, user_language.upper())}**. "
        f"This is the user's profile language; honor it even if the "
        f"intent text below contains other-language words. Translate "
        f"any non-{_LANG_NAMES_FOR_PROMPT.get(user_language, user_language.upper())} fragments into "
        f"{_LANG_NAMES_FOR_PROMPT.get(user_language, user_language.upper())} when you weave them in.",
        "- Real emails have STRUCTURE. Use blank-line separated paragraphs — "
          "don't write one wall of text.",
        "- Open with a greeting appropriate to the tone "
          "(e.g. 'Hi <name>,' / 'Hallo <name>,' / 'Sehr geehrte/r <Name>,'). "
          "If the recipient's name isn't in the address, use a generic opening.",
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
        "- Don't invent facts the user didn't mention.",
        "- Each draft must be complete and ready-to-send as-is.",
        "",
    ]

    if state_key and state_key in state_specs:
        spec = state_specs[state_key]
        lines.extend([
            f"═══ TONE: {spec['label_en']} / {spec['label_de']} ═══",
            spec["tone"],
            "",
            "All 3 drafts share this tone. They DIFFER only in angle / "
            "framing / level of detail — not in tone.",
            "",
        ])
    else:
        lines.append("═══ THREE ANGLES TO COVER ═══")
        for i, (_label, spec) in enumerate(variant_specs, 1):
            lines.append(f"  DRAFT {i}: {spec}")
        lines.append("")

    if style_examples:
        lines.append("═══ HOW THE USER WRITES TO THIS PERSON ═══")
        lines.append("(Most recent emails the user themselves sent — match this style)")
        for body in style_examples[-5:]:
            lines.append(f"  • {body}")
        lines.append("")

    if subject:
        lines.append("═══ SUBJECT LINE (already set by the user) ═══")
        lines.append(subject.strip())
        lines.append("(Don't repeat the subject in the body.)")
        lines.append("")

    if attachments:
        lines.append("═══ ATTACHMENTS THE USER IS SENDING WITH THIS EMAIL ═══")
        lines.append("HARD RULES for attachments:")
        lines.append("- DON'T invent context, narrative, or description for "
                     "any attachment. You can see the filenames and types but "
                     "NOT what's actually in the photos or how they were "
                     "taken. NEVER write things like \"from our weekend\", "
                     "\"from the trip\", \"that I took at\" unless the "
                     "user's intent below explicitly states it.")
        lines.append("- For PHOTOS: a single neutral mention is enough — e.g. "
                     "\"I've attached a couple of photos\" or \"see the "
                     "attached photos\". Don't elaborate, don't speculate.")
        lines.append("- For DOCUMENTS/PDFs: a real text snippet may appear "
                     "below the filename — use it for one short, factual "
                     "sentence (\"I'm attaching the suit price list — prices "
                     "start at €399\"). NO snippet → use the filename as the "
                     "hint and DON'T speculate on contents.")
        lines.append("- If the user's intent doesn't suggest mentioning the "
                     "attachments at all, DON'T force a mention.")
        lines.append("")
        for a in attachments:
            fn = (a.get("filename") or "").strip() or "attachment"
            mt = (a.get("mimetype") or "").strip()
            snippet = (a.get("snippet") or "").strip()
            kind = ""
            if mt.startswith("image/"):
                kind = "photo"
            elif mt == "application/pdf":
                kind = "PDF document"
            elif mt.startswith("text/"):
                kind = "text file"
            elif mt:
                kind = mt
            tag = f" ({kind})" if kind else ""
            lines.append(f"  • {fn}{tag}")
            if snippet:
                # Wrap so the LLM sees the snippet as a quoted block,
                # not a continuation of the bullet list. Limit per the
                # caller's truncation (~400 chars).
                lines.append(f"      content snippet: \"{snippet}\"")
        lines.append("")

    lines.extend([
        "═══ USER'S INTENT (what to say) ═══",
        intent.strip() or "(no intent provided — write a polite hello)",
        "",
    ])

    if request_subject:
        lines.extend([
            "═══ OUTPUT FORMAT (STRICT) ═══",
            "FIRST line: SUBJECT: <a concise subject line, < 60 chars, in the "
            "user's language, no quotes>. Then the three drafts below.",
            "",
            "SUBJECT: <subject line here>",
            "---DRAFT 1---",
            "<email body with paragraphs separated by blank lines, greeting, "
            "sign-off + name>",
            "---DRAFT 2---",
            "<email body>",
            "---DRAFT 3---",
            "<email body>",
            "",
            "Output:",
        ])
    else:
        lines.extend([
            "═══ OUTPUT FORMAT (STRICT) ═══",
            "Three drafts, EXACTLY in this format, nothing else (the user "
            "has already set the subject — body only).",
            "---DRAFT 1---",
            "<email body with paragraphs separated by blank lines, greeting, "
            "sign-off + name>",
            "---DRAFT 2---",
            "<email body>",
            "---DRAFT 3---",
            "<email body>",
            "",
            "Drafts:",
        ])
    return "\n".join(lines)
