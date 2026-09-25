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
from typing import Any, Optional

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


HEURISTIC_VERSION = "h1"


def _user_classifier_mode(conn, user_id: Any) -> str:
    """Resolve the user's classifier preference. Defaults to heuristic
    so existing behaviour doesn't change silently when the column is
    NULL (e.g. on fresh installs that haven't run migration 117 yet)."""
    try:
        row = conn.execute(
            "SELECT classifier_mode FROM user_profiles WHERE id = ?",
            (user_id,),
        ).fetchone()
        return (row["classifier_mode"] if row and row["classifier_mode"] else "heuristic")
    except Exception:  # noqa: BLE001
        return "heuristic"


def apply_to_message(message_id: int, *, quiet: bool = False) -> Optional[str]:
    """Look up the message, classify it (per the owner's preference),
    persist the category. Returns the category — callers that want
    to act on the result (e.g. propose 'New bill — add to bills?')
    don't have to re-fetch.

    `quiet` is for mail brought in by an import or a repair pass: the
    heuristic only (no model call per old mail) and no bell proposal —
    a bill from last spring is not news."""
    from .database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT subject, body_text, snippet, from_email, from_name, owner_user_id "
            "FROM email_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if not row:
            return None
        mode = _user_classifier_mode(conn, row["owner_user_id"])
        body_text = row["body_text"] or ""
        snippet   = row["snippet"] or ""
        # Many HTML-only mailers have an empty / near-empty body_text
        # because the alternative-text part is missing. Fall back to
        # the snippet (the pre-header / first lines that the inbox
        # shows alongside the subject) so the classifier always sees
        # the message's most informative inline text, not just the
        # subject + sender. When body_text already has substance,
        # appending the snippet adds the pre-header headline that
        # often signals the category ("Your invoice is attached" vs
        # "20% off everything!").
        if len(body_text.strip()) < 200 and snippet:
            llm_body = (snippet + "\n\n" + body_text).strip()
        else:
            llm_body = body_text
        # LLM path: try the LLM, fall back to heuristic on any failure
        # (network blip, parse miss, unknown category). Belt-and-braces:
        # the heuristic always runs at least as a fallback, so a flaky
        # LLM endpoint never leaves rows uncategorised.
        category: Optional[str] = None
        version = HEURISTIC_VERSION
        if mode == "llm" and not quiet:
            from . import email_classifier_llm as _llm
            category = _llm.classify_llm(
                row["subject"] or "",
                llm_body,
                row["from_email"] or "",
                row["from_name"] or "",
            )
            if category is not None:
                version = _llm.LLM_VERSION
        if category is None:
            category = classify(
                row["subject"] or "", body_text, row["from_email"] or "",
            )
        conn.execute(
            "UPDATE email_messages SET category = ?, classifier_version = ? WHERE id = ?",
            (category, version, message_id),
        )
    log.debug("classified message %s → %s (mode=%s, version=%s)", message_id, category, mode, version)
    # For actionable categories (bill / appointment), surface a one-click
    # proposal in the notification bell. The user accepts → we run the
    # existing add_bill / add_calendar_event skill with extracted data.
    if category in ("bill", "appointment") and not quiet:
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

# A number with thousands groups ("1.234,56", "1,234.56") or without
# ("1234.56", "214,00"). Which mark is the decimal one is decided in
# _parse_amount, not here — the same pattern serves German and US mail.
_NUM = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?|\d+(?:[.,]\d{2})?"
_AMOUNT_RE = _re.compile(
    rf"(?:(€|\$|£|EUR|USD|GBP)\s*({_NUM}))"
    rf"|(?:({_NUM})\s*(€|\$|£|EUR|USD|GBP))"
)
# Words in front of the amount that was actually charged. A receipt
# lists subtotal, tax and line items too; the first amount in the text
# is often one of those.
_TOTAL_STRONG_RE = _re.compile(
    r"amount\s+(?:paid|due|charged)|grand\s+total|balance\s+due|"
    r"rechnungsbetrag|gesamtbetrag|endbetrag|zahlbetrag|zu\s+zahlen",
    _re.IGNORECASE,
)
_TOTAL_WEAK_RE = _re.compile(r"\b(?:total|gesamt|summe)\b(?!\s*(?:excl|exkl|netto|ohne|before))", _re.IGNORECASE)


def _parse_amount(s: str) -> float:
    """'214.00', '214,00', '1,234.56', '1.234,56', '1.234' → float.
    The last mark is the decimal point only when exactly two digits
    follow it; every other mark separates thousands."""
    last = max(s.rfind("."), s.rfind(","))
    if last != -1 and len(s) - last - 1 == 2:
        whole, cents = s[:last], s[last + 1:]
    else:
        whole, cents = s, "0"
    return float(whole.replace(".", "").replace(",", "") + "." + cents)


def _pick_amount(text: str):
    """The charged amount: one labelled 'amount paid'/'Rechnungsbetrag'
    beats one labelled 'total', which beats the first amount found."""
    best, best_score, prev_end = None, -1, 0
    for m in _AMOUNT_RE.finditer(text):
        label = text[max(prev_end, m.start() - 40):m.start()]
        score = 2 if _TOTAL_STRONG_RE.search(label) else 1 if _TOTAL_WEAK_RE.search(label) else 0
        if score > best_score:
            best, best_score = m, score
        prev_end = m.end()
    return best
# The due date is the first date within a few words after one of these.
_DUE_KW_RE = _re.compile(
    r"\b(?:f[äa]llig(?:keit)?(?:keitsdatum)?|zahlbar\s+bis|zahlungsziel|"
    r"due|pay(?:ment)?\s+by|payable\s+by)\b",
    _re.IGNORECASE,
)
_DATE_SLASH_RE = _re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b(?!/)")   # 10/05/2026, 10/5
_DATE_DE_RE = _re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})?\b")
_DATE_ISO_RE = _re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_TIME_RE = _re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(uhr|am|pm)?\b", _re.IGNORECASE)


def _extract_bill(text: str) -> dict:
    """Best-effort {amount, currency, due_date} from email text."""
    out: dict = {}
    m = _pick_amount(text)
    if m:
        cur = m.group(1) or m.group(4) or "EUR"
        try:
            out["amount"] = _parse_amount(m.group(2) or m.group(3) or "")
            out["currency"] = {"€": "EUR", "$": "USD", "£": "GBP"}.get(cur, cur).upper()
        except ValueError:
            pass
    for kw in _DUE_KW_RE.finditer(text):
        # Blank out amounts so "due €12.10 on …" doesn't read as 12 Oct.
        window = _AMOUNT_RE.sub(lambda a: " " * len(a.group(0)), text[kw.end():kw.end() + 40])
        if (due := _first_date(window, us=out.get("currency") == "USD")):
            out["due_date"] = due
            break
    return out


def _bill_llm_enabled(owner_id: Any) -> bool:
    """user_profiles.bill_extract_llm — on unless the user turned it off."""
    from .database import get_conn
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT bill_extract_llm FROM user_profiles WHERE id = ?", (owner_id,),
            ).fetchone()
        return True if row is None or row["bill_extract_llm"] is None else bool(row["bill_extract_llm"])
    except Exception as exc:  # noqa: BLE001 — column not migrated yet
        log.debug("bill_extract_llm lookup failed: %s", exc)
        return True


_ANY_NUM_RE = _re.compile(rf"(?<![\d.,])(?:{_NUM})(?![\d])")


def _amount_in_text(amount: float, text: str) -> bool:
    """Does `amount` appear in the mail, in whatever notation?"""
    return any(abs(_parse_amount(m.group(0)) - amount) < 0.005 for m in _ANY_NUM_RE.finditer(text))


def _extract_bill_llm(text: str, row: dict) -> Optional[dict]:
    """The model's reading of the bill, kept only where it checks out:
    the amount must stand in the mail, the currency is an ISO code, the
    due date a real date within a year either way. None when the model
    is off, unreachable or its amount fails the check — then the rules
    decide alone."""
    from datetime import date, timedelta
    from . import email_classifier_llm as _llm
    got = _llm.extract_bill_llm(row.get("subject") or "", text,
                                row.get("from_email") or "", row.get("from_name") or "")
    if not got:
        return None
    try:
        amount = round(float(got.get("amount")), 2)
    except (TypeError, ValueError):
        return None
    if amount <= 0 or not _amount_in_text(amount, text):
        log.info("LLM bill amount %r not found in mail — using the rules", got.get("amount"))
        return None
    out: dict = {"amount": amount, "source": "llm"}
    cur = str(got.get("currency") or "").strip().upper()
    if _re.fullmatch(r"[A-Z]{3}", cur):
        out["currency"] = cur
    try:
        due = date.fromisoformat(str(got.get("due_date") or ""))
        if abs((due - date.today()).days) <= 366:
            out["due_date"] = due.isoformat()
    except ValueError:
        pass    # null or garbage: a receipt has no due date
    return out


def _first_date(text: str, us: bool, today=None) -> Optional[str]:
    """The earliest date in `text` as ISO: 2026-10-05, 05.10.2026,
    10/05/2026, October 5, 2026, 5. Oktober 2026. A slash date is read
    month-first in a dollar bill and day-first otherwise, unless one of
    the two numbers can only be a day. Without a year: this year, or
    next year when that date lies more than 60 days back."""
    from .email_invites import (_DATE_DMY_NAME_RE, _DATE_ISO_RE, _DATE_MDY_NAME_RE,
                                _DATE_NUM_RE, _MONTHS, _valid)
    from datetime import date, timedelta
    today = today or date.today()

    def build(y: Optional[str], mo: int, d: int) -> Optional[str]:
        if y:
            return _valid(int(y) + (2000 if len(y) == 2 else 0), mo, d)
        iso = _valid(today.year, mo, d)
        if iso and iso < (today - timedelta(days=60)).isoformat():
            iso = _valid(today.year + 1, mo, d)
        return iso

    found: list[tuple[int, str]] = []
    for m in _DATE_ISO_RE.finditer(text):
        found.append((m.start(), _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))))
    for m in _DATE_NUM_RE.finditer(text):
        found.append((m.start(), build(m.group(3), int(m.group(2)), int(m.group(1)))))
    for m in _DATE_DMY_NAME_RE.finditer(text):
        found.append((m.start(), build(m.group(3), _MONTHS[m.group(2).lower()], int(m.group(1)))))
    for m in _DATE_MDY_NAME_RE.finditer(text):
        found.append((m.start(), build(m.group(3), _MONTHS[m.group(1).lower()], int(m.group(2)))))
    for m in _DATE_SLASH_RE.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        month_first = b > 12 or (us and a <= 12)
        mo, d = (a, b) if month_first else (b, a)
        found.append((m.start(), build(m.group(3), mo, d)))
    hits = sorted((pos, iso) for pos, iso in found if iso)
    return hits[0][1] if hits else None


def _extract_appointment(text: str) -> dict:
    """Best-effort {date, time, end_time} from email text (month names,
    AM/PM and ranges included — see email_invites.extract_appointment)."""
    from .email_invites import extract_appointment
    return extract_appointment(text)


def _propose_action(message_id: int, category: str, row: dict) -> None:
    """Create a 'one-click add' notification for a classified email.
    The notification carries enough payload that the Accept handler can
    dispatch directly to add_bill / add_calendar_event without
    re-reading the email body."""
    from . import notifications, email_blocklist
    # Drop early if the user has blocked this sender (or its domain).
    # No notification, no row change — we still pulled the mail, the
    # user just won't be bothered about it.
    owner_id = str(row["owner_user_id"])
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
        if _bill_llm_enabled(owner_id) and (llm := _extract_bill_llm(text, row)):
            # The model's answer as a whole — its "no due date" on a
            # receipt included; the rules fill only a missing currency.
            extracted = {**llm, "currency": llm.get("currency") or extracted.get("currency") or "EUR"}
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
        user_id=str(row["owner_user_id"]),
        kind="email_proposal",
        title=title,
        body=body,
        payload=payload,
        navigate_to=f"/r/email?msg={message_id}",
    )
