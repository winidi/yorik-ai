"""Finance dashboard round 2: recurring-payment detection (pure
function, no DB) + focus-categories preference + the /recurring
endpoint's visibility (same rule as everything else: private stays
private, shared is shared, no admin exception).

See docs/plans/2026-09-25-finanzen.md "Runde 2"."""

from __future__ import annotations

import pytest

from backend.bank_accounts import detect_recurring
from tests.conftest import login_client


def _tx(account_id, date, amount, counterparty, category=None):
    return {
        "account_id": account_id, "booking_date": date, "amount": amount,
        "counterparty": counterparty, "purpose": "", "category": category,
    }


def test_two_months_same_amount_same_counterparty_is_recurring():
    rows = [
        _tx(1, "2026-07-15", -9.99, "Spotify AB"),
        _tx(1, "2026-08-15", -9.99, "Spotify AB"),
        _tx(1, "2026-09-15", -9.99, "Spotify AB"),
    ]
    out = detect_recurring(rows)
    assert len(out) == 1
    assert out[0]["counterparty"] == "Spotify AB"
    assert out[0]["months_seen"] == 3
    assert out[0]["avg_interval_days"] == 31


def test_single_occurrence_is_not_recurring():
    rows = [_tx(1, "2026-09-15", -9.99, "Spotify AB")]
    assert detect_recurring(rows) == []


def test_wildly_varying_amount_is_not_recurring():
    # Same supermarket every week, but grocery bills swing wildly --
    # that's a habit, not a subscription, and shouldn't clutter Verträge.
    rows = [
        _tx(1, "2026-07-03", -12.40, "Rewe Peine"),
        _tx(1, "2026-08-10", -87.20, "Rewe Peine"),
    ]
    assert detect_recurring(rows) == []


def test_incoming_payments_are_never_recurring():
    rows = [
        _tx(1, "2026-07-25", 3200.00, "Arbeitgeber GmbH"),
        _tx(1, "2026-08-25", 3200.00, "Arbeitgeber GmbH"),
    ]
    assert detect_recurring(rows) == []


def test_different_accounts_stay_separate_groups():
    rows = [
        _tx(1, "2026-07-15", -9.99, "Spotify AB"),
        _tx(2, "2026-08-15", -9.99, "Spotify AB"),
    ]
    # each account only saw it once -> neither is recurring on its own
    assert detect_recurring(rows) == []


def test_small_amount_tolerance_still_counts_as_recurring():
    # FX/rounding jitter within the 1 EUR floor should not disqualify it.
    rows = [
        _tx(1, "2026-07-15", -9.99, "Spotify AB"),
        _tx(1, "2026-08-15", -10.49, "Spotify AB"),
    ]
    out = detect_recurring(rows)
    assert len(out) == 1


@pytest.fixture
def two_users_with_accounts(fresh_app):
    from backend import spaces as S
    from backend.database import get_conn

    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate")):
        S.ensure_personal_space(uid, name)

    with get_conn() as conn:
        def _acct(owner, name):
            cur = conn.execute(
                "INSERT INTO bank_accounts "
                "(owner_user_id, space_id, display_name, bank_url, blz, login_name, credential_key) "
                "VALUES (?, NULL, ?, 'https://example.invalid', '00000000', 'x', 'unused') RETURNING id",
                (owner, name),
            )
            return int(cur.fetchone()["id"])

        dirk_acct = _acct(dirk, "Dirks Konto")
        beate_acct = _acct(beate, "Beates Konto")

        def _txn(account_id, date, amount, counterparty):
            conn.execute(
                "INSERT INTO bank_transactions (account_id, booking_date, amount, counterparty, dedup_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_id, date, amount, counterparty, f"{account_id}-{date}-{counterparty}"),
            )

        for d in ("2026-07-15", "2026-08-15", "2026-09-15"):
            _txn(dirk_acct, d, -9.99, "Spotify AB")
        for d in ("2026-07-20", "2026-08-20"):
            _txn(beate_acct, d, -14.99, "Fitnessstudio")
        conn.commit()

    return {"dirk_c": dirk_c, "beate_c": beate_c, "dirk": dirk, "beate": beate}


def test_recurring_endpoint_only_shows_own_private_account(two_users_with_accounts):
    t = two_users_with_accounts
    r = t["dirk_c"].get("/api/bank/recurring")
    assert r.status_code == 200
    names = [row["counterparty"] for row in r.json()]
    assert "Spotify AB" in names
    assert "Fitnessstudio" not in names


def test_focus_categories_default_then_roundtrip(fresh_app):
    client, _uid = login_client(fresh_app, role="member", name="Dirk", email="d2@example.local")
    r = client.get("/api/bank/focus-categories")
    assert r.status_code == 200
    assert r.json()["categories"] == ["Lebensmittel", "Verträge & Abos"]

    r = client.put("/api/bank/focus-categories", json={"categories": ["Auto", "Freizeit"]})
    assert r.status_code == 200
    assert r.json()["categories"] == ["Auto", "Freizeit"]

    r = client.get("/api/bank/focus-categories")
    assert r.json()["categories"] == ["Auto", "Freizeit"]


def test_focus_categories_caps_at_max_and_ignores_blanks(fresh_app):
    client, _uid = login_client(fresh_app, role="member", name="Dirk", email="d3@example.local")
    r = client.put("/api/bank/focus-categories", json={"categories": ["A", " ", "B", "C", "D", "E"]})
    assert r.json()["categories"] == ["A", "B", "C", "D"]
