"""Finance app — pull transactions via FinTS, dedupe, categorise, store.

Chat skills and the Finance app read ONLY the local bank_transactions
table, never FinTS live — a sync happens here, on account creation and
on a schedule, so a chat question never has to wait on a bank's FinTS
server (or risk a TAN prompt mid-conversation).

v1 categorisation is a small hardcoded keyword table — enough to prove
the column and the UI filter work. Replace with a user-editable rules
table (+ LLM fallback for anything unmatched) once there's real
transaction history to calibrate against; see
docs/plans/2026-09-25-finanzen.md "Später".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Optional

from . import credential_store, workers
from . import bank_accounts as bank_accounts_mod
from .bank_accounts import dedup_hash
from .database import get_conn

log = logging.getLogger("yorik.bank_sync")

try:
    from fints.client import FinTS3PinTanClient  # type: ignore
    _AVAILABLE = True
except ImportError:
    FinTS3PinTanClient = None  # type: ignore
    _AVAILABLE = False

SYNC_INTERVAL_S = 6 * 3600  # every 6 hours, matches other Yorik sync workers
INITIAL_SYNC_DAYS = 180     # first sync per account: 6 months back
ROUTINE_SYNC_DAYS = 14      # later syncs: overlap a couple weeks, dedup_hash catches repeats

_CATEGORY_RULES: list[tuple[str, str]] = [
    # (substring in counterparty/purpose, lowercase) -> category
    ("rewe", "Lebensmittel"), ("edeka", "Lebensmittel"), ("aldi", "Lebensmittel"),
    ("lidl", "Lebensmittel"), ("kaufland", "Lebensmittel"), ("netto", "Lebensmittel"),
    ("miete", "Wohnen"), ("nebenkosten", "Wohnen"), ("hausverwaltung", "Wohnen"),
    ("stadtwerke", "Wohnen"), ("strom", "Wohnen"), ("gas ", "Wohnen"),
    ("versicherung", "Versicherung"),
    ("telekom", "Telekommunikation"), ("vodafone", "Telekommunikation"), ("o2", "Telekommunikation"),
    ("netflix", "Abos"), ("spotify", "Abos"), ("amazon prime", "Abos"),
    ("tankstelle", "Auto"), ("shell", "Auto"), ("aral", "Auto"), ("esso", "Auto"),
    ("gehalt", "Einkommen"), ("lohn", "Einkommen"), ("gutschrift", "Einkommen"),
]


def _categorize(counterparty: str, purpose: str) -> Optional[str]:
    haystack = f"{counterparty or ''} {purpose or ''}".lower()
    for needle, category in _CATEGORY_RULES:
        if needle in haystack:
            return category
    return None


def _load_account(account_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bank_accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def sync_account(account_id: int) -> dict[str, Any]:
    """Blocking — call via asyncio.to_thread from request handlers, or
    directly from the scheduler thread below."""
    if not _AVAILABLE:
        return {"ok": False, "error": "python-fints not installed"}
    acct = _load_account(account_id)
    if not acct or not acct.get("enabled", 1):
        return {"ok": False, "error": "account not found or disabled"}
    creds = credential_store.get(acct["credential_key"])
    if not creds:
        return {"ok": False, "error": "no PIN in credential store"}

    days = INITIAL_SYNC_DAYS if not acct.get("last_synced_at") else ROUTINE_SYNC_DAYS
    end = date.today()
    start = end - timedelta(days=days)

    try:
        client = FinTS3PinTanClient(
            acct["blz"], acct["login_name"], creds["pin"], acct["bank_url"],
            product_id=acct.get("product_id") or bank_accounts_mod._FALLBACK_TEST_PRODUCT_ID,
        )
        inserted = 0
        with client:
            accounts = client.get_sepa_accounts()
            target = accounts[0] if accounts else None
            if target is None:
                raise RuntimeError("no SEPA account returned by this bank login")
            txs = client.get_transactions(target, start, end)
            with get_conn() as conn:
                for t in txs:
                    d = t.data if hasattr(t, "data") else {}
                    booking_date = d.get("date")
                    amount_obj = d.get("amount")
                    if not booking_date or not amount_obj:
                        continue
                    amount = float(amount_obj.amount)
                    counterparty = d.get("applicant_name") or ""
                    purpose = d.get("purpose") or ""
                    h = dedup_hash(booking_date.isoformat(), amount, counterparty, purpose)
                    category = _categorize(counterparty, purpose)
                    try:
                        conn.execute(
                            "INSERT INTO bank_transactions "
                            "(account_id, booking_date, amount, currency, counterparty, purpose, "
                            " posting_text, category, dedup_hash) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT (account_id, dedup_hash) DO NOTHING",
                            (account_id, booking_date.isoformat(), amount,
                             amount_obj.currency or "EUR", counterparty, purpose,
                             d.get("posting_text"), category, h),
                        )
                        inserted += 1
                    except Exception as exc:  # noqa: BLE001
                        log.warning("bank_sync: insert failed for account %s: %s", account_id, exc)
                conn.commit()
            iban = getattr(target, "iban", None)
        with get_conn() as conn:
            conn.execute(
                "UPDATE bank_accounts SET last_synced_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS'), "
                "last_sync_error = NULL, iban = COALESCE(?, iban) WHERE id = ?",
                (iban, account_id),
            )
            conn.commit()
        return {"ok": True, "fetched": len(txs), "inserted_or_skipped": inserted}
    except Exception as exc:  # noqa: BLE001
        log.warning("bank_sync: sync failed for account %s: %s", account_id, exc)
        with get_conn() as conn:
            conn.execute("UPDATE bank_accounts SET last_sync_error = ? WHERE id = ?",
                        (f"{type(exc).__name__}: {exc}", account_id))
            conn.commit()
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _sync_all_enabled() -> tuple[int, int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM bank_accounts WHERE enabled = 1").fetchall()
    ok = 0
    for r in rows:
        result = sync_account(int(r["id"]))
        if result.get("ok"):
            ok += 1
    return ok, len(rows)


_task = None


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    global _task
    workers.register("bank-sync", kind="scheduler", expected_interval_s=SYNC_INTERVAL_S)

    async def _loop():
        while True:
            try:
                ok, total = await asyncio.get_running_loop().run_in_executor(None, _sync_all_enabled)
                workers.heartbeat("bank-sync", "ok" if ok == total else "warn",
                                   f"{ok}/{total} accounts synced")
            except Exception as exc:  # noqa: BLE001
                log.warning("bank-sync: scheduler tick failed: %s", exc)
                workers.heartbeat("bank-sync", "error", str(exc))
            await asyncio.sleep(SYNC_INTERVAL_S)

    _task = loop.create_task(_loop(), name="bank-sync-scheduler")
