"""payments_to: bank bookings and receipts matched into single payments,
totals by code (chat rerun 2026-09-27: "was hab ich für Claude bezahlt"
gave 345,02 €, 559,22 € and 604,42 € in six runs)."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from tests.conftest import seed_user


def _ctx(uid):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role="admin", user_id=uid)


RECEIPTS = {
    # mail subject → what the model reads out of it
    "Your receipt from Anthropic #1": {"is_bill": True, "payee": "Anthropic", "amount": "€107.10",
                                       "currency": "EUR", "date": "D30", "paid": True, "method": "Visa"},
    "Your receipt from Anthropic #2": {"is_bill": True, "payee": "Anthropic", "amount": "€214.20",
                                       "currency": "EUR", "date": "D3", "paid": True, "method": "Mastercard - 8600"},
    "Anthropic invoice due": {"is_bill": True, "payee": "Anthropic", "amount": "€50.00",
                              "currency": "EUR", "date": "D1", "paid": False},
    "Anthropic newsletter": {"is_bill": False},
    "Anthropic wrong amount": {"is_bill": True, "payee": "Anthropic", "amount": "999.99", "currency": "EUR",
                               "date": "D2", "paid": True},
}


@pytest.fixture
def house(fresh_app, monkeypatch):
    from backend import payments, search_routes, spaces
    from backend.database import get_conn
    uid = seed_user(name="Dirk", role="admin", email="dirk@example.com")
    spaces.ensure_workspace_exists(uid, "Dirk")
    spaces.ensure_personal_space(uid, "Dirk")
    today = date.today()
    d = lambda n: (today - timedelta(days=n)).isoformat()
    bodies = {"Your receipt from Anthropic #1": "Amount paid €107.10", "Your receipt from Anthropic #2": "€214.20 paid",
              "Anthropic invoice due": "Please pay €50.00", "Anthropic newsletter": "News from Anthropic",
              "Anthropic wrong amount": "Total €19.99"}
    with get_conn() as conn:
        acc = conn.execute("INSERT INTO bank_accounts (owner_user_id, space_id, display_name, bank_url, blz, login_name, "
                           "credential_key) VALUES (?, NULL, 'Giro', 'https://example.invalid', '0', 'x', 'u') RETURNING id",
                           (uid,)).fetchone()["id"]
        for i, (days, amt, cp, purpose) in enumerate((
                (28, -107.10, "VISA ANTHROPIC* CLAUDE SUB", ""),          # the receipt of D30, booked 2 days later
                (60, -107.10, "VISA ANTHROPIC* CLAUDE SUB", ""),          # no receipt
                (27, -0.90, "Kleingeld Plus - Sparen", "Aus Kauf107,10. beiANTHROPIC"),  # round-up: not a payment
                (5, 20.00, "ANTHROPIC refund", ""))):                     # money in: not a payment
            conn.execute("INSERT INTO bank_transactions (account_id, booking_date, amount, counterparty, purpose, category, "
                         "dedup_hash) VALUES (?, ?, ?, ?, ?, 'Abos', ?)", (acc, d(days), amt, cp, purpose, f"h{i}"))
        mail_acc = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                                "smtp_username, credential_key) VALUES (?, 'd@example.local', 'i', 'u', 's', 'u', 'k') "
                                "RETURNING id", (uid,)).fetchone()["id"]
        mail_ids = []
        for n, subject in enumerate(RECEIPTS):
            mail_ids.append(conn.execute(
                "INSERT INTO email_messages (account_id, uid, owner_user_id, message_id, from_email, subject, body_text, snippet) "
                "VALUES (?, ?, ?, ?, 'invoice@anthropic.com', ?, ?, '') RETURNING id",
                (mail_acc, n + 1, uid, f"<m{n}@x>", subject, bodies[subject])).fetchone()["id"])
        conn.commit()

    async def search(q, user):
        hits = [{"source": "email", "id": i, "title": s} for i, s in zip(mail_ids, RECEIPTS)]
        return {"query": q, "total": len(hits), "results": {"email": hits}}
    monkeypatch.setattr(search_routes, "universal_search", search)

    asked = []
    async def read(prompt, max_tokens=200):
        subject = next(s for s in RECEIPTS if s in prompt)
        asked.append(subject)
        facts = dict(RECEIPTS[subject])
        if str(facts.get("date", "")).startswith("D"):
            facts["date"] = d(int(facts["date"][1:]))
        return facts
    monkeypatch.setattr(payments, "_chat_json", read)
    return {"uid": uid, "asked": asked, "d": d}


def test_receipts_and_bookings_become_single_payments(house):
    from backend.skills.payments_to.skill import execute
    out = asyncio.run(execute(_ctx(house["uid"]), payee="Claude", also=["Anthropic"]))
    got = [(p["date"], p["amount"], p["status"]) for p in out["payments"]]
    d = house["d"]
    assert got == [
        (d(1), "-50,00 €", "open"),                 # the receipt asks for payment
        (d(3), "-214,20 €", "paid_elsewhere"),      # paid, but not over this account (Mastercard)
        (d(28), "-107,10 €", "paid_bank"),          # receipt D30 and booking D28: one payment
        (d(60), "-107,10 €", "bank_only"),          # a booking without receipt
    ]
    assert out["totals"]["paid"] == "428,40 €" and out["totals"]["open"] == "50,00 €"
    assert "Kleingeld" not in str(out) and "refund" not in str(out)
    assert "Say which were paid another way" in out["_llm_hint"]


def test_a_receipt_is_read_once_and_an_amount_not_in_its_text_is_not_taken(house):
    from backend.database import get_conn
    from backend.skills.payments_to.skill import execute
    asyncio.run(execute(_ctx(house["uid"]), payee="Anthropic"))
    first = list(house["asked"])
    asyncio.run(execute(_ctx(house["uid"]), payee="Anthropic"))
    assert house["asked"] == first                                   # kept in receipt_facts
    with get_conn() as conn:
        wrong = conn.execute("SELECT amount_cents FROM receipt_facts f JOIN email_messages m ON m.id = f.ref_id "
                             "WHERE m.subject = 'Anthropic wrong amount'").fetchone()
    assert wrong["amount_cents"] is None                             # 999.99 does not stand in the mail


def test_amount_spellings():
    from backend.payments import cents, _in_text, money
    assert cents("€1.234,56") == 123456 and cents("$4.00") == 400 and cents("214,20") == 21420
    assert cents("1,234.56") == 123456 and cents("") is None
    assert _in_text(21420, "Amount paid €214.20") and _in_text(21420, "Betrag 214,20 EUR")
    assert _in_text(123456, "Summe 1.234,56 €") and not _in_text(21420, "€1214.20")
    assert money(-55922) == "-559,22 €" and money(400, "USD") == "$4.00" and money(400, "CHF") == "4,00 CHF"


def test_number_in_the_purpose_matches_and_reminders_are_one_claim():
    """netcup: reminder nc-5260955 on 14.06., paid 07.07. "Rechnung
    nc.5260955" (23 days: outside the date window). Riverty: two
    reminders of one file number, paid with fees."""
    from backend.payments import match
    receipts = [
        {"source": "email", "title": "Zahlungserinnerung nc-5260955", "link": "/m/1", "amount_cents": 8668,
         "currency": "EUR", "bill_date": "2026-06-14", "paid": False, "number": "nc-5260955", "payee": "netcup"},
        {"source": "email", "title": "Offene Forderung AZ S.26.5130182.01.7", "link": "/m/2", "amount_cents": 12019,
         "currency": "EUR", "bill_date": "2026-08-27", "paid": False, "number": "S.26.5130182.01.7",
         "ref": "265130182017", "earlier_notices": 1, "payee": "Riverty"},
    ]
    bookings = [
        {"booking_date": "2026-07-07", "amount": -86.68, "counterparty": "netcup GmbH", "purpose": "Rechnung nc.5260955"},
        {"booking_date": "2026-09-25", "amount": -120.25, "counterparty": "Riverty Services GmbH",
         "purpose": "Aktenzeichen. S.26.5130182.01.7"},
    ]
    got = [(p["date"], p["amount_cents"], p["status"]) for p in match(bookings, receipts)]
    assert got == [("2026-09-25", -12025, "paid_bank"), ("2026-07-07", -8668, "paid_bank")]


def test_invoice_and_receipt_of_one_payment_are_paid(house):
    """Anthropic sends an invoice ("due") and a receipt ("paid") with the
    same amount and day: one payment, paid."""
    from backend.database import get_conn
    from backend.skills.payments_to.skill import execute
    d = house["d"]
    with get_conn() as conn:
        conn.execute("UPDATE email_messages SET body_text = '€214.20 due, pay online' "
                     "WHERE subject = 'Anthropic invoice due'")
        conn.commit()
    RECEIPTS["Anthropic invoice due"].update({"amount": "€214.20", "date": "D3"})
    try:
        out = asyncio.run(execute(_ctx(house["uid"]), payee="Anthropic"))
    finally:
        RECEIPTS["Anthropic invoice due"].update({"amount": "€50.00", "date": "D1"})
    sept = [p for p in out["payments"] if p["date"] == d(3)]
    assert [(p["amount"], p["status"]) for p in sept] == [("-214,20 €", "paid_elsewhere")]
    assert out["totals"]["open"] == "0,00 €"


def test_ref_digits_ignores_dates():
    from backend.payments import ref_digits
    assert ref_digits("AZ: S.26.5130182.01.7") == "265130182017"
    assert ref_digits("Rechnung nc-5260955") == "5260955"
    assert ref_digits("Your receipt 2026-09-24") == "" and ref_digits("Mahnung vom 24.09.2026") == ""


def test_before_the_bank_records_and_reminders_without_number():
    from backend.payments import match
    receipts = [
        {"source": "email", "title": "receipt", "link": "/m/1", "amount_cents": 10710, "currency": "EUR",
         "bill_date": "2026-06-17", "paid": True, "payee": "Anthropic"},
        {"source": "email", "title": "invoice", "link": "/m/2", "amount_cents": 2113, "currency": "EUR",
         "bill_date": "2026-06-01", "paid": False, "payee": "Riverty"},
    ]
    got = {p["amount_cents"]: p["status"] for p in match([], receipts, records_start="2026-06-26")}
    assert got == {-10710: "paid_before_records", -2113: "unclear"}


def test_three_mails_about_one_bill_are_one_bill_monthly_bills_stay():
    from backend.payments import group_bills
    mk = lambda day, paid=False: {"source": "email", "title": f"May invoice {day}", "link": "/m", "amount_cents": 2113,
                                  "currency": "EUR", "bill_date": day, "paid": paid, "payee": "Riverty"}
    got = group_bills([mk("2026-06-01"), mk("2026-06-15"), mk("2026-06-19"), mk("2026-07-19")])
    assert sorted(g["bill_date"] for g in got) == ["2026-06-19", "2026-07-19"]
    assert next(g for g in got if g["bill_date"] == "2026-06-19")["earlier_notices"] == 2


def test_without_payee_only_the_unsettled_bills(house):
    """#35 "welche Rechnungen muss ich noch bezahlen": the model guessed
    payees and alternated netcup / Malerbetrieb 26 times."""
    from backend.skills.payments_to.skill import execute
    out = asyncio.run(execute(_ctx(house["uid"])))
    assert [(p["amount"], p["status"]) for p in out["payments"]] == [("-50,00 €", "open")]
    assert out["totals"]["open"] == "50,00 €" and out["bills_checked"] == 3


def test_without_any_bank_account_nothing_is_called_open():
    """Beate has no bank account in Yorik: her bill is unclear, not open."""
    from backend.payments import match
    bill = {"source": "email", "title": "Rechnung", "link": "/m/1", "amount_cents": 2299, "currency": "EUR",
            "bill_date": "2026-09-10", "paid": False, "payee": "Buhl"}
    paid = {**bill, "amount_cents": 1000, "paid": True}
    got = {p["amount_cents"]: p["status"] for p in match([], [bill, paid], records_start=None)}
    assert got == {-2299: "unclear", -1000: "paid_before_records"}
