"""Heuristic email classifier.

Runs in the email_fetcher hot path so every newly-ingested email gets a
category. We use lightweight regex/keyword rules instead of calling the
LLM per email — cheaper, faster, and reliable enough for the use case
(displaying a colored badge in the email list, suggesting "add to bills?"
for invoices). LLM-based classification is a future upgrade if rules
prove too noisy in practice.

Categories:
  bill          — invoices / payment requests / Rechnungen
  appointment   — meeting confirmations / Terminbestätigungen
  newsletter    — bulk mail with unsubscribe footer
  notification  — automated alerts (banking, social, transactional)
  personal      — human-written, addressed to a person
  other         — fallback when nothing matches
"""
from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger("homeos.email_classifier")


# ── Heuristic patterns. All matched against subject + body (lowercased). ──

# Strong invoice signals — currency amount + due-date or "invoice/Rechnung".
_BILL_TERMS = re.compile(
    r"\b(invoice|rechnung|bill|amount\s*due|betrag|zahlung|payment\s*due|fällig|"
    r"rechnungsnummer|invoice\s*number|due\s*date|fälligkeitsdatum|payable)\b",
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(r"(€|\$|£|EUR|USD|GBP)\s*\d+[,.]?\d{0,2}|\d+[,.]?\d{0,2}\s*(€|\$|£|EUR|USD)", re.IGNORECASE)

# Appointment / meeting confirmation signals.
_APPT_TERMS = re.compile(
    r"\b(meeting|appointment|termin|reservation|booking|"
    r"confirmation|bestätigung|reminder.*meeting|see you at|wir treffen uns|"
    r"calendar invite|.ics|conference call|teams meeting|zoom meeting|google meet)\b",
    re.IGNORECASE,
)
_TIME_OF_DAY_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\s*(am|pm|uhr)?\b", re.IGNORECASE)

# Newsletter signals — unsubscribe footers, "view in browser", weekly digests.
_NEWSLETTER_TERMS = re.compile(
    r"\b(unsubscribe|abmelden|newsletter|view\s+in\s+browser|im\s+browser\s+anzeigen|"
    r"weekly\s+digest|monthly\s+update|view\s+as\s+web\s*page|email\s+preferences)\b",
    re.IGNORECASE,
)

# Transactional / system notifications (banking, security, social).
_NOTIFICATION_TERMS = re.compile(
    r"\b(password\s+(reset|changed)|security\s+alert|new\s+sign[\- ]in|"
    r"login\s+from|verification\s+code|2fa|two[\- ]factor|"
    r"account\s+(update|alert|notification)|noreply|no[\- ]reply|do[\- ]not[\- ]reply)\b",
    re.IGNORECASE,
)


def classify(subject: str, body: str, from_email: str = "") -> str:
    """Return one of: bill / appointment / newsletter / notification /
    personal / other. Pure heuristic — no LLM, no DB."""
    text = " ".join(filter(None, [subject or "", body or ""])).lower()
    sender = (from_email or "").lower()

    # Order matters: strong signals first. "bill" beats "newsletter" if both
    # match (an invoice newsletter is still a bill).
    if _BILL_TERMS.search(text) and _CURRENCY_RE.search(text):
        return "bill"

    if _APPT_TERMS.search(text) and _TIME_OF_DAY_RE.search(text):
        return "appointment"

    # noreply senders → almost always notification, even if they say
    # "your invoice is attached" (those are usually receipts not action
    # items — distinguished from real bills by being already-paid).
    if any(tok in sender for tok in ("noreply", "no-reply", "donotreply", "no.reply")):
        if _BILL_TERMS.search(text):
            # Receipt rather than bill, but route to notification — user
            # doesn't need to do anything.
            return "notification"
        return "notification"

    if _NEWSLETTER_TERMS.search(text):
        return "newsletter"

    if _NOTIFICATION_TERMS.search(text):
        return "notification"

    # Default: assume personal correspondence. The email_app surfaces
    # these without a badge so they don't all get a colored chip.
    if subject or body:
        return "personal"
    return "other"


def backfill_all(limit: int = 5000) -> int:
    """One-shot: classify any message that doesn't have a category yet.
    Called once at startup so existing messages get badges too — without
    waiting for new IMAP traffic. Limit caps the work per call so the
    first boot after this lands doesn't hang on a giant inbox.
    Note: backfill does NOT create proposal notifications for old mail —
    we don't want to flood the bell with 50 entries on first boot. Only
    newly-arriving classified mail proposes."""
    from .database import get_conn
    n = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, subject, body_text, from_email "
            "FROM email_messages WHERE category IS NULL OR category = '' "
            "LIMIT ?",
            (int(limit),),
        ).fetchall()
        for r in rows:
            cat = classify(r["subject"] or "", r["body_text"] or "", r["from_email"] or "")
            conn.execute("UPDATE email_messages SET category = ? WHERE id = ?", (cat, r["id"]))
            n += 1
    if n:
        log.info("backfilled category for %d messages", n)
    return n


def apply_to_message(message_id: int) -> Optional[str]:
    """Look up the message, classify it, persist the category. Returns
    the category (also for callers that want to act on it immediately,
    e.g. push a 'New bill — add to bills?' notification)."""
    from .database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT subject, body_text, from_email, owner_user_id "
            "FROM email_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if not row:
            return None
        category = classify(row["subject"] or "", row["body_text"] or "", row["from_email"] or "")
        conn.execute("UPDATE email_messages SET category = ? WHERE id = ?", (category, message_id))
    log.debug("classified message %s → %s", message_id, category)
    # For actionable categories (bill / appointment), surface a one-click
    # proposal in the notification bell. The user accepts → we run the
    # existing add_bill / add_calendar_event skill with extracted data.
    if category in ("bill", "appointment"):
        try:
            _propose_action(message_id, category, dict(row))
        except Exception as exc:  # noqa: BLE001
            log.debug("propose-action for msg %s failed: %s", message_id, exc)
    return category


# ── Proposal extraction ──────────────────────────────────────────────
# Regex-only for v1. The fields we extract are best-effort suggestions;
# if extraction fails we still create the notification with whatever we
# have — the Accept action shows a confirmation modal where the user
# can correct anything before it lands in bills/calendar.

import re as _re
from datetime import datetime as _dt

_AMOUNT_RE = _re.compile(
    r"(?:(€|\$|£|EUR|USD|GBP)\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?))"
    r"|(?:(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*(€|\$|£|EUR|USD|GBP))"
)
_DUE_DE_RE = _re.compile(r"\bf[äa]llig(?:keit)?(?:keitsdatum)?\s*(?:am|:)?\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})?", _re.IGNORECASE)
_DUE_EN_RE = _re.compile(r"\bdue\s*(?:date|by|on)?\s*[:\-]?\s*(\d{4})-(\d{1,2})-(\d{1,2})", _re.IGNORECASE)
_DATE_DE_RE = _re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})?\b")
_DATE_ISO_RE = _re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_TIME_RE = _re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(uhr|am|pm)?\b", _re.IGNORECASE)


def _extract_bill(text: str) -> dict:
    """Best-effort {amount, currency, due_date} from email text."""
    out: dict = {}
    m = _AMOUNT_RE.search(text)
    if m:
        cur = m.group(1) or m.group(4) or "EUR"
        amt = (m.group(2) or m.group(3) or "").replace(".", "").replace(",", ".")
        try:
            out["amount"] = float(amt)
            out["currency"] = {"€": "EUR", "$": "USD", "£": "GBP"}.get(cur, cur).upper()
        except ValueError:
            pass
    m = _DUE_DE_RE.search(text)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3) or str(_dt.now().year)
        if len(y) == 2:
            y = "20" + y
        out["due_date"] = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    elif (m := _DUE_EN_RE.search(text)):
        out["due_date"] = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return out


def _extract_appointment(text: str) -> dict:
    """Best-effort {date, time} from email text."""
    out: dict = {}
    m = _DATE_ISO_RE.search(text)
    if m:
        out["date"] = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    elif (m := _DATE_DE_RE.search(text)):
        d, mo, y = m.group(1), m.group(2), m.group(3) or str(_dt.now().year)
        if len(y) == 2:
            y = "20" + y
        out["date"] = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m = _TIME_RE.search(text)
    if m:
        out["time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
    return out


def _propose_action(message_id: int, category: str, row: dict) -> None:
    """Create a 'one-click add' notification for a classified email.
    The notification carries enough payload that the Accept handler can
    dispatch directly to add_bill / add_calendar_event without
    re-reading the email body."""
    from . import notifications, email_blocklist
    # Drop early if the user has blocked this sender (or its domain).
    # No notification, no row change — we still pulled the mail, the
    # user just won't be bothered about it.
    owner_id = int(row["owner_user_id"])
    from_email_raw = (row.get("from_email") or "").strip()
    if email_blocklist.matches(owner_id, from_email_raw):
        log.info("skipped %s notification — sender %r is blocked",
                 category, from_email_raw)
        return
    text = " ".join(filter(None, [row.get("subject"), row.get("body_text")]))
    sender = (row.get("from_email") or "").split("@")[0].strip().title() or "sender"
    subj = (row.get("subject") or "(no subject)")[:60]

    if category == "bill":
        extracted = _extract_bill(text)
        amount_s = (f"{extracted['amount']:.2f} {extracted.get('currency', 'EUR')}"
                    if "amount" in extracted else "amount unknown")
        due_s = f", due {extracted['due_date']}" if "due_date" in extracted else ""
        title = f"New bill from {sender}?"
        body  = f"{amount_s}{due_s}. From email: \"{subj}\""
        payload = {
            "message_id":   message_id,
            "category":     "bill",
            "extracted":    extracted,
            "vendor":       sender,
            "from_email":   from_email_raw,   # raw address — drives the spam button
            "subject":      subj,
        }
    else:  # appointment
        extracted = _extract_appointment(text)
        when = " ".join(filter(None, [extracted.get("date"), extracted.get("time")])) or "time unknown"
        title = f"New appointment with {sender}?"
        body  = f"{when}. From email: \"{subj}\""
        payload = {
            "message_id":   message_id,
            "category":     "appointment",
            "extracted":    extracted,
            "with":         sender,
            "from_email":   from_email_raw,   # raw address — drives the spam button
            "subject":      subj,
        }

    notifications.create(
        user_id=int(row["owner_user_id"]),
        kind="email_proposal",
        title=title,
        body=body,
        payload=payload,
        navigate_to=f"/r/email?msg={message_id}",
    )
