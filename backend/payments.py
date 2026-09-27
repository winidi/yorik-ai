"""Everything paid to one payee: bank bookings and receipts matched into
single payments, totals by code.

Chat rerun 2026-09-27: "was hab ich für Claude bezahlt" gave 345,02 €,
559,22 €, 604,42 € — the model chose each time which bookings and which
receipts to add up, missed the receipt paid by another card, and once
counted round-ups to savings. Here the model only reads each receipt
(amount, date, paid or open) once; matching and adding up is code.

A receipt and a booking are one payment when the amounts agree (1 %, a
foreign currency 0.75–1.35 for the exchange rate) and the booking lies
3 days before to 10 days after the receipt's date.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("yorik.payments")

EXTRACT_PROMPT = (
    "Read this document and say whether it is a bill, invoice or receipt, and what it says. "
    "Answer only JSON: {{\"is_bill\": true|false, \"payee\": \"who is paid\", "
    "\"amount\": \"the total paid or to pay, exactly as written\", \"currency\": \"EUR, USD, …\", "
    "\"date\": \"YYYY-MM-DD of the bill or the payment\", "
    "\"paid\": true if it says it is paid, false if it asks for payment, null if it does not say, "
    "\"method\": \"the card or account it was paid with, as written, or null\", "
    "\"number\": \"invoice or receipt number, or null\"}}\n\nDocument:\n{text}")
EXTRACT_TIMEOUT_S = 40.0
MAX_TEXT = 4000
MAX_CANDIDATES = 12


# ─── amounts ─────────────────────────────────────────────────────────

def cents(raw: Any) -> Optional[int]:
    """"214.20", "214,20", "€1.234,56", "$4.00" → cents."""
    s = re.sub(r"[^\d.,]", "", str(raw or ""))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s.rsplit(",", 1)[1]) == 2 else s.replace(",", "")
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return None


def _in_text(amount_cents: int, text: str) -> bool:
    """The amount stands in the text in some usual spelling — what the
    model read must be there, not worked out."""
    euros, rest = divmod(abs(amount_cents), 100)
    forms = {f"{euros}.{rest:02d}", f"{euros},{rest:02d}",
             f"{euros:,}.{rest:02d}", f"{euros:,}.{rest:02d}".replace(",", "X").replace(".", ",").replace("X", ".")}
    flat = text.replace(" ", " ")
    return any(re.search(rf"(?<![\d.,]){re.escape(f)}(?![\d])", flat) for f in forms)


def money(amount_cents: int, currency: str = "EUR") -> str:
    s = f"{abs(amount_cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    sign = "-" if amount_cents < 0 else ""
    return f"{sign}{s} €" if (currency or "EUR").upper() == "EUR" else f"{sign}{s} {currency.upper()}"


# ─── reading a receipt ───────────────────────────────────────────────

async def _chat_json(prompt: str, max_tokens: int = 200) -> Optional[Dict[str, Any]]:
    """One short answer from the local model, as JSON. Module-level so
    tests swap it."""
    import httpx
    from .agent.llm import _thinking_kwargs_enabled
    body: Dict[str, Any] = {"messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.0, "max_tokens": max_tokens}
    if os.getenv("HOMEOS_MODEL"):
        body["model"] = os.getenv("HOMEOS_MODEL")
    if _thinking_kwargs_enabled():
        body["chat_template_kwargs"] = {"enable_thinking": False}
        body["reasoning_effort"] = "none"
    base = os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    try:
        async with httpx.AsyncClient(timeout=EXTRACT_TIMEOUT_S) as client:
            r = await client.post(f"{base}/chat/completions", json=body,
                                  headers={"Authorization": "Bearer not-used"})
        r.raise_for_status()
        raw = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        found = re.search(r"\{.*\}", raw, re.S)
        return json.loads(found.group(0)) if found else None
    except Exception as exc:  # noqa: BLE001
        log.warning("payments: reading a receipt failed: %s", exc)
        return None


def _iso(v: Any) -> Optional[str]:
    try:
        return date.fromisoformat(str(v or "")[:10]).isoformat()
    except ValueError:
        return None


async def read_receipt(source: str, ref_id: int, text: str) -> Optional[Dict[str, Any]]:
    """What the receipt says, read once and kept (receipt_facts). None
    when the model could not be asked; then nothing is stored."""
    from .database import get_conn
    text = (text or "")[:MAX_TEXT]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM receipt_facts WHERE source = ? AND ref_id = ?",
                           (source, int(ref_id))).fetchone()
    if row and row["text_hash"] == digest:
        return dict(row)
    got = await _chat_json(EXTRACT_PROMPT.format(text=text))
    if got is None:
        return None
    amount = cents(got.get("amount"))
    if amount is not None and not _in_text(amount, text):
        amount = None                            # not in the text: not taken
    paid = got.get("paid") if isinstance(got.get("paid"), bool) else None
    facts = {"source": source, "ref_id": int(ref_id), "text_hash": digest,
             "is_bill": bool(got.get("is_bill")), "amount_cents": amount,
             "currency": (str(got.get("currency") or "EUR").strip().upper()[:3] or "EUR"),
             "bill_date": _iso(got.get("date")), "paid": paid,
             "method": (str(got["method"])[:80] if got.get("method") else None),
             "number": (str(got["number"])[:80] if got.get("number") else None),
             "payee": (str(got["payee"])[:120] if got.get("payee") else None)}
    with get_conn() as conn:
        conn.execute("DELETE FROM receipt_facts WHERE source = ? AND ref_id = ?", (source, int(ref_id)))
        conn.execute(
            "INSERT INTO receipt_facts (source, ref_id, text_hash, is_bill, amount_cents, currency, bill_date, "
            "paid, method, number, payee) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(facts[k] for k in ("source", "ref_id", "text_hash", "is_bill", "amount_cents", "currency",
                                     "bill_date", "paid", "method", "number", "payee")))
        conn.commit()
    return facts


# ─── gathering ───────────────────────────────────────────────────────

async def _bank(ctx, names: List[str], since: str, until: str) -> List[Dict[str, Any]]:
    """Bookings whose payee or purpose names one of the names; round-ups
    to savings are left out by show_transactions."""
    from .skills.show_transactions.skill import execute as show
    rows: Dict[Tuple, Dict[str, Any]] = {}
    for name in names:
        out = await show(ctx, days=365, search=name, from_date=since, to_date=until)
        for t in out.get("transactions") or []:
            if float(t.get("amount") or 0) >= 0:
                continue                         # money coming in is no payment
            key = (t["booking_date"], t["amount"], t.get("counterparty"), t.get("purpose"))
            rows.setdefault(key, t)
    return list(rows.values())


def _records_start(ctx) -> Optional[str]:
    """First booking day on the accounts the person may see: before it
    the bank cannot say whether a receipt was paid over them."""
    from . import spaces
    from .database import get_conn
    frag, params = spaces.row_filter(str(ctx.user_id), getattr(ctx, "role", None), "bank_accounts", table_alias="a")
    with get_conn() as conn:
        r = conn.execute(f"SELECT MIN(t.booking_date) AS first FROM bank_transactions t "
                         f"JOIN bank_accounts a ON a.id = t.account_id WHERE {frag}", params).fetchone()
    return str(r["first"])[:10] if r and r["first"] else None


def _email_text(user_id: str, mail_id: int) -> Optional[Dict[str, Any]]:
    from .database import get_conn
    with get_conn() as conn:
        r = conn.execute("SELECT id, subject, from_name, from_email, date_received, body_text, snippet "
                         "FROM email_messages WHERE id = ? AND owner_user_id = ?",
                         (int(mail_id), user_id)).fetchone()
    if not r:
        return None
    text = "\n".join(str(x) for x in (r["subject"], r["from_name"] or r["from_email"], r["date_received"],
                                        r["body_text"] or r["snippet"]) if x)
    return {"title": r["subject"] or "", "text": text, "link": f"/r/email?msg={r['id']}"}


def _paperless_text(user_id: str, doc_id: int) -> Optional[Dict[str, Any]]:
    from . import paperless_ingest as PI
    creds = PI.user_creds(user_id)
    if not creds:
        return None
    doc = PI._fetch_doc(int(doc_id), creds_override=creds)          # as the person: their permissions
    if not doc or not doc.get("content"):
        return None
    return {"title": doc.get("title") or "", "text": f"{doc.get('title') or ''}\n{doc['content']}",
            "link": f"/r/documents?doc={int(doc_id)}&source=paperless"}


async def _receipts(ctx, names: List[str]) -> List[Dict[str, Any]]:
    """Receipts and invoices in documents and mail that name the payee."""
    import asyncio
    from .search_routes import universal_search
    user_id = str(ctx.user_id)
    user = {"id": user_id, "role": getattr(ctx, "role", None) or "member"}
    # The name alone brings notifications ("GitHub Copilot: What's in your
    # free plan"); with the words of a bill the receipts come too.
    queries = [q for n in names for q in (n, f"{n} Rechnung", f"{n} receipt invoice")]
    runs = await asyncio.gather(*(universal_search(q=q, user=user) for q in queries))
    seen, candidates = set(), []
    for run in runs:
        for source in ("paperless", "email"):
            for h in (run.get("results") or {}).get(source) or []:
                key = (source, h.get("id"))
                if h.get("id") is None or key in seen:
                    continue
                seen.add(key)
                candidates.append(key)
    low = [n.lower() for n in names]
    out = []
    for source, ref in candidates[:MAX_CANDIDATES * 2]:
        doc = (_paperless_text(user_id, ref) if source == "paperless" else _email_text(user_id, ref))
        if not doc or not any(n in doc["text"].lower() for n in low):
            continue                              # a search neighbour that does not name the payee
        facts = await read_receipt(source, int(ref), doc["text"])
        # "Payment Receipt" came back is_bill false, paid true (GitHub)
        if facts and (facts["is_bill"] or facts["paid"] is True) and facts["amount_cents"]:
            out.append({**facts, "title": doc["title"], "link": doc["link"]})
        if len(out) >= MAX_CANDIDATES:
            break
    return group_bills(out)


def group_bills(out: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per bill: duplicates, invoice and receipt, reminders."""
    # The same bill as mail and as filed document, or invoice and receipt
    # of one payment (Anthropic: "due" and "paid", same amount and day):
    # one entry, paid when any of them says paid.
    unique: Dict[Tuple, Dict[str, Any]] = {}
    for r in sorted(out, key=lambda r: r["source"] != "paperless"):
        key = (r["amount_cents"], r["currency"], r["bill_date"])
        if key in unique:
            first = unique[key]
            first.setdefault("also_in", []).append({"title": r["title"], "link": r["link"]})
            if r.get("paid") is True:
                first["paid"], first["method"] = True, first.get("method") or r.get("method")
        else:
            unique[key] = r
    # Reminders of one claim carry its number (Riverty's file number,
    # netcup's invoice number): the newest is the claim.
    claims: Dict[str, Dict[str, Any]] = {}
    rest: List[Dict[str, Any]] = []
    for r in sorted(unique.values(), key=lambda r: r.get("bill_date") or "", reverse=True):
        key = ref_digits(r.get("number")) or ref_digits(r.get("title"))
        if key and key in claims:
            claims[key]["earlier_notices"] = claims[key].get("earlier_notices", 0) + 1
            if r.get("paid") is True:
                claims[key]["paid"] = True
            continue
        if key:
            r["ref"] = key
            claims[key] = r
        else:
            rest.append(r)
    # "ready for payment", "due in 4 days", "overdue": one bill without a
    # number (Riverty's May invoice, three mails). Monthly bills of the
    # same amount lie 28 days or more apart.
    merged: List[Dict[str, Any]] = []
    for r in sorted(rest, key=lambda r: r.get("bill_date") or "", reverse=True):
        twin = next((m for m in merged if r.get("paid") is not True and m.get("paid") is not True
                     and m["amount_cents"] == r["amount_cents"] and m.get("currency") == r.get("currency")
                     and r.get("bill_date") and m.get("bill_date")
                     and 0 <= (date.fromisoformat(m["bill_date"]) - date.fromisoformat(r["bill_date"])).days <= 25),
                    None)
        if twin:
            twin["earlier_notices"] = twin.get("earlier_notices", 0) + 1
        else:
            merged.append(r)
    return list(claims.values()) + merged


def ref_digits(text: Any) -> str:
    """The longest run of digits in an invoice or file number, dots and
    dashes ignored ("S.26.5130182.01.7" → "265130182017",
    "nc-5260955" → "5260955"); at least 6 digits, else nothing."""
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:+.]*)?|\b\d{1,2}\.\d{1,2}\.\d{2,4}\b", " ", str(text or ""))
    runs = re.findall(r"\d[\d.\-/]*\d", text)
    best = max((re.sub(r"\D", "", r) for r in runs), key=len, default="")
    return best if len(best) >= 6 else ""


# ─── matching ────────────────────────────────────────────────────────

def _fits(receipt: Dict[str, Any], booking: Dict[str, Any]) -> bool:
    b = abs(cents(booking["amount"]) or 0)
    r = receipt["amount_cents"]
    if (receipt.get("currency") or "EUR") == (booking.get("currency") or "EUR").upper():
        ok = abs(b - r) <= max(1, r // 100)
    else:
        ok = 0.75 <= b / r <= 1.35 if r else False
    if not ok or not receipt.get("bill_date"):
        return False                 # without a date, 107,10 € in July and in August are indistinguishable
    lag = (date.fromisoformat(booking["booking_date"][:10]) - date.fromisoformat(receipt["bill_date"])).days
    return -3 <= lag <= 10


def _by_number(receipt: Dict[str, Any], booking: Dict[str, Any]) -> bool:
    """The bill's number stands in the booking's purpose ("Rechnung
    nc.5260955", "Aktenzeichen S.26.5130182.01.7"): the payment of that
    bill, whatever the fees added, up to 60 days later."""
    key = receipt.get("ref") or ref_digits(receipt.get("number"))
    if not key or key not in re.sub(r"\D", "", booking.get("purpose") or ""):
        return False
    if not receipt.get("bill_date"):
        return True
    lag = (date.fromisoformat(booking["booking_date"][:10]) - date.fromisoformat(receipt["bill_date"])).days
    return -10 <= lag <= 60


def match(bookings: List[Dict[str, Any]], receipts: List[Dict[str, Any]],
          records_start: Optional[str] = None) -> List[Dict[str, Any]]:
    free = list(bookings)
    payments: List[Dict[str, Any]] = []
    for r in sorted(receipts, key=lambda r: r.get("bill_date") or ""):
        hit = next((b for b in free if _by_number(r, b)), None) or next((b for b in free if _fits(r, b)), None)
        receipt_src = {"source": r["source"], "title": r["title"], "navigate_to": r["link"],
                       **({"payment_method": r["method"]} if r.get("method") else {})}
        if hit:
            free.remove(hit)
            amount = cents(hit["amount"])
            payments.append({"date": hit["booking_date"][:10], "amount_cents": -abs(amount), "currency": "EUR",
                             "status": "paid_bank", "what": r.get("payee") or hit.get("counterparty") or "",
                             "sources": [receipt_src, _bank_src(hit)]})
        else:
            before = bool(records_start and r.get("bill_date") and r["bill_date"] < records_start)
            if before:
                status = "paid_before_records" if r.get("paid") is True else "unclear"
            else:
                status = {True: "paid_elsewhere", False: "open"}.get(r.get("paid"), "unclear")
            payments.append({"date": r.get("bill_date") or "", "amount_cents": -r["amount_cents"],
                             "currency": r.get("currency") or "EUR", "status": status,
                             "what": r.get("payee") or r["title"], "sources": [receipt_src]})
    for b in free:
        payments.append({"date": b["booking_date"][:10], "amount_cents": -abs(cents(b["amount"]) or 0),
                         "currency": "EUR", "status": "bank_only", "what": (b.get("counterparty") or "").strip(),
                         "sources": [_bank_src(b)]})
    return sorted(payments, key=lambda p: p["date"], reverse=True)


def _bank_src(b: Dict[str, Any]) -> Dict[str, Any]:
    return {"source": "bank", "counterparty": (b.get("counterparty") or "").strip(),
            "booking_date": b["booking_date"][:10], "account": b.get("account_name") or ""}


STATUS_TEXT = {"paid_bank": "bezahlt, auf dem Konto", "paid_elsewhere": "bezahlt, nicht über dieses Konto",
               "paid_before_records": "bezahlt laut Beleg, vor Beginn der Kontodaten",
               "open": "offen laut Beleg, keine Zahlung gefunden", "unclear": "Beleg, Zahlung nicht gefunden",
               "bank_only": "Kontobuchung ohne Beleg"}


def totals(payments: List[Dict[str, Any]]) -> Dict[str, Any]:
    paid: Dict[str, int] = {}
    open_: Dict[str, int] = {}
    for p in payments:
        bucket = open_ if p["status"] == "open" else paid if p["status"] != "unclear" else None
        if bucket is not None:
            bucket[p["currency"]] = bucket.get(p["currency"], 0) + abs(p["amount_cents"])
    fmt = lambda d: " + ".join(money(v, k) for k, v in sorted(d.items(), key=lambda kv: kv[0] != "EUR")) or money(0)
    return {"paid": fmt(paid), "open": fmt(open_),
            "paid_count": sum(1 for p in payments if p["status"] not in ("open", "unclear")),
            "open_count": sum(1 for p in payments if p["status"] == "open")}


async def payments_to(ctx, payee: str, also: Optional[List[str]] = None,
                      from_date: Optional[str] = None, to_date: Optional[str] = None) -> Dict[str, Any]:
    names = []
    for n in [payee, *(also or [])]:
        n = str(n or "").strip()
        if n and n.lower() not in [x.lower() for x in names]:
            names.append(n)
    names = names[:4]
    until = _iso(to_date) or date.today().isoformat()
    since = _iso(from_date) or (date.fromisoformat(until) - timedelta(days=365)).isoformat()
    bookings = await _bank(ctx, names, since, until)
    receipts = [r for r in await _receipts(ctx, names)
                if not r.get("bill_date") or since <= r["bill_date"] <= until]
    start = _records_start(ctx)
    pays = match(bookings, receipts, start)
    rows = [{"date": p["date"], "amount": money(p["amount_cents"], p["currency"]), "status": p["status"],
             "status_text": STATUS_TEXT[p["status"]], "what": p["what"], "sources": p["sources"]}
            for p in pays]
    return {"payee": payee, "searched_as": names, "from": since, "to": until, "bank_records_from": start,
            "totals": totals(pays), "payments": rows}
