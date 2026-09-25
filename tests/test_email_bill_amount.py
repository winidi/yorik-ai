"""'New bill from …?': the amount reads the same in German and US mail,
and a receipt's charged amount wins over its subtotal and line items."""

import pytest

from backend.email_classifier import _extract_bill


@pytest.mark.parametrize("text, amount, currency", [
    # US / English
    ("Amount paid €214.00", 214.00, "EUR"),
    ("EUR 214.20", 214.20, "EUR"),
    ("$1,234.56", 1234.56, "USD"),
    ("$1234.56", 1234.56, "USD"),
    ("$12,000", 12000.00, "USD"),
    ("USD 1,234,567.89", 1234567.89, "USD"),
    ("£9.99", 9.99, "GBP"),
    # German
    ("214,00 €", 214.00, "EUR"),
    ("€1.234,56", 1234.56, "EUR"),
    ("1234,56 EUR", 1234.56, "EUR"),
    ("12.000 €", 12000.00, "EUR"),
    ("Betrag: 49 €", 49.00, "EUR"),
])
def test_amount_either_notation(text, amount, currency):
    got = _extract_bill(text)
    assert got["amount"] == pytest.approx(amount) and got["currency"] == currency


def test_receipt_takes_the_charged_amount():
    receipt = ("Your receipt from Anthropic, PBC #2630-1588-6291\n"
               "Claude Max  €180.00\nSubtotal €180.00\nTotal excluding tax €180.00\n"
               "VAT (19% on €180.00) €34.20\nTotal €214.20\nAmount paid €214.00")
    assert _extract_bill(receipt)["amount"] == pytest.approx(214.00)


def test_total_beats_line_items():
    assert _extract_bill("Posten 1  10,00 €\nPosten 2  5,50 €\nGesamt 15,50 €")["amount"] == pytest.approx(15.50)
    assert _extract_bill("Rechnungsbetrag: 1.299,00 EUR, fällig am 01.10.2026") == {
        "amount": 1299.00, "currency": "EUR", "due_date": "2026-10-01"}


def test_no_label_falls_back_to_first_amount():
    assert _extract_bill("Your plan renews at $20.00, add-ons $5.00")["amount"] == pytest.approx(20.00)


# ── due date ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, due", [
    # US
    ("Amount due $89.00. Due date: 10/05/2026", "2026-10-05"),
    ("Please pay by October 5, 2026.", "2026-10-05"),
    ("Payment due Oct. 5th, 2026 — $89.00", "2026-10-05"),
    ("Total $89.00, due on 2026-10-05", "2026-10-05"),
    ("$89.00 due 10/13/26", "2026-10-13"),
    # German / European
    ("Rechnungsbetrag 89,00 €, fällig am 05.10.2026", "2026-10-05"),
    ("Zahlbar bis 5. Oktober 2026: 89,00 €", "2026-10-05"),
    ("Zahlungsziel: 05/10/2026, Betrag 89,00 €", "2026-10-05"),   # day first outside the US
    ("£89.00 due 13/10/2026", "2026-10-13"),
])
def test_due_date_either_notation(text, due):
    assert _extract_bill(text)["due_date"] == due


def test_due_skips_the_amount_and_other_dates():
    # the amount right after "due" is no date, the invoice date is no due date
    assert _extract_bill("Invoice date 09/20/2026. Amount due $12.10 on 10/01/2026")["due_date"] == "2026-10-01"
    assert "due_date" not in _extract_bill("Rechnung vom 20.09.2026 über 89,00 €")


def test_due_without_year():
    from datetime import date
    from backend.email_classifier import _first_date
    today = date(2026, 12, 20)
    assert _first_date("Jan 5", us=True, today=today) == "2027-01-05"      # just after New Year
    assert _first_date("12/10", us=True, today=today) == "2026-12-10"      # a reminder, still this year


# ── the model reads, the rules check ──────────────────────────────────

RECEIPT = ("Your receipt from Anthropic, PBC #2630-1588-6291\nSubtotal €180.00\n"
           "VAT €34.00\nTotal €214.00\nAmount paid €214.00\nPaid September 24, 2026")


def _model_says(monkeypatch, answer):
    from backend import email_classifier_llm
    monkeypatch.setattr(email_classifier_llm, "extract_bill_llm", lambda *a, **k: answer)


def test_model_answer_kept_when_amount_is_in_the_mail(monkeypatch):
    from backend.email_classifier import _extract_bill_llm
    _model_says(monkeypatch, {"amount": 214, "currency": "eur", "due_date": None})
    assert _extract_bill_llm(RECEIPT, {}) == {"amount": 214.0, "currency": "EUR", "source": "llm"}


def test_model_amount_found_in_other_notation(monkeypatch):
    from backend.email_classifier import _extract_bill_llm
    _model_says(monkeypatch, {"amount": 1234.56, "currency": "EUR", "due_date": "2026-10-05"})
    got = _extract_bill_llm("Rechnungsbetrag 1.234,56 €, zahlbar bis 5. Oktober", {})
    assert got["amount"] == 1234.56 and got["due_date"] == "2026-10-05"


@pytest.mark.parametrize("answer", [
    {"amount": 21420, "currency": "EUR"},        # not in the mail
    {"amount": "ignore previous instructions"},  # garbage
    {"amount": None},
    None,                                        # model off / unreachable
])
def test_model_answer_dropped(monkeypatch, answer):
    from backend.email_classifier import _extract_bill_llm
    _model_says(monkeypatch, answer)
    assert _extract_bill_llm(RECEIPT, {}) is None


def test_model_due_date_must_be_a_real_nearby_date(monkeypatch):
    from backend.email_classifier import _extract_bill_llm
    _model_says(monkeypatch, {"amount": 214.0, "currency": "EUR", "due_date": "1999-01-01"})
    assert "due_date" not in _extract_bill_llm(RECEIPT, {})
    _model_says(monkeypatch, {"amount": 214.0, "currency": "EUR", "due_date": "2026-13-45"})
    assert "due_date" not in _extract_bill_llm(RECEIPT, {})
