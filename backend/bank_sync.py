"""Finance app — pull transactions via FinTS, dedupe, categorise, store.

Chat skills and the Finance app read ONLY the local bank_transactions
table, never FinTS live — a sync happens here, on account creation and
on a schedule, so a chat question never has to wait on a bank's FinTS
server (or risk a TAN prompt mid-conversation).

Categorisation is two-stage: a small hardcoded keyword table catches
the obvious cases for free and instantly; anything it misses goes to
the same local LLM Yorik's own chat uses (one batched call per sync,
not one call per transaction) — see _categorize_missing_via_llm() and
docs/plans/2026-09-25-finanzen.md "Runde 2".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
    ("netflix", "Verträge & Abos"), ("spotify", "Verträge & Abos"), ("amazon prime", "Verträge & Abos"),
    ("tankstelle", "Auto"), ("shell", "Auto"), ("aral", "Auto"), ("esso", "Auto"),
    ("gehalt", "Einkommen"), ("lohn", "Einkommen"), ("gutschrift", "Einkommen"),
]


def _categorize(counterparty: str, purpose: str) -> Optional[str]:
    haystack = f"{counterparty or ''} {purpose or ''}".lower()
    for needle, category in _CATEGORY_RULES:
        if needle in haystack:
            return category
    return None


# Fixed category set the LLM fallback picks from, in German to match
# the rest of the Finance UI. "Verträge & Abos" replaces the old plain
# "Abos" bucket -- broader (also insurance-adjacent recurring services
# that aren't a classic subscription) so it lines up with the
# dashboard's "Verträge & Abos" quick-stat and the /recurring endpoint.
_LLM_CATEGORIES = [
    "Lebensmittel", "Wohnen", "Versicherung", "Telekommunikation",
    "Verträge & Abos", "Auto", "Freizeit", "Gesundheit", "Einkommen", "Sonstiges",
]
_LLM_BATCH_SIZE = 30
_LLM_TIMEOUT_S = 45


def _categorize_missing_via_llm(entries: list[dict[str, Any]]) -> dict[int, str]:
    """entries: [{"idx": int, "counterparty": str, "purpose": str, "amount": float}, ...]
    for whatever the keyword pass left uncategorised. One call per
    _LLM_BATCH_SIZE entries against Yorik's own local model -- read-only
    classification, so a wrong guess is low-stakes (wrong bucket, not
    wrong money moved). Falls back to no categorisation at all (not a
    crash) if the local LLM is unreachable."""
    if not entries:
        return {}
    try:
        from .agent.llm import LlmClient
    except Exception as exc:  # noqa: BLE001
        log.warning("bank_sync: LlmClient unavailable, skipping AI categorisation: %s", exc)
        return {}

    client = LlmClient(
        model=os.getenv("HOMEOS_MODEL", "qwen3.8-27b"),
        base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        request_timeout=_LLM_TIMEOUT_S,
    )
    out: dict[int, str] = {}
    for start in range(0, len(entries), _LLM_BATCH_SIZE):
        chunk = entries[start:start + _LLM_BATCH_SIZE]
        lines = [
            "Assign each bank transaction below to exactly ONE category from this fixed "
            f"list: {_LLM_CATEGORIES!r}. Use the amount sign as a hint (negative = money "
            "out, positive = money in -- positive amounts are almost always 'Einkommen'). "
            "If truly nothing fits, use 'Sonstiges'. Never invent a new category name.",
            "",
            "=== TRANSACTIONS ===",
        ]
        for e in chunk:
            lines.append(
                f"{e['idx']}: counterparty={e['counterparty']!r} purpose={e['purpose']!r} "
                f"amount={e['amount']:.2f} EUR"
            )
        lines.append(
            "\n=== OUTPUT ===\n"
            "Strict JSON only: {\"categories\": {\"<idx>\": \"<category>\", ...}}, one entry "
            "per transaction above. No markdown, no commentary."
        )
        try:
            resp = client.chat(
                messages=[{"role": "user", "content": "\n".join(lines)}],
                max_tokens=1500,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("bank_sync: AI categorisation call failed: %s", exc)
            continue
        try:
            parsed = json.loads((resp.get("content") or "").strip())
            cats = parsed.get("categories") if isinstance(parsed, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            cats = None
        if not isinstance(cats, dict):
            continue
        for idx_str, cat in cats.items():
            try:
                idx = int(idx_str)
            except (TypeError, ValueError):
                continue
            if cat in _LLM_CATEGORIES:
                out[idx] = cat
    return out


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

            # Pass 1: parse + free keyword categorisation.
            parsed = []
            for t in txs:
                d = t.data if hasattr(t, "data") else {}
                booking_date = d.get("date")
                amount_obj = d.get("amount")
                if not booking_date or not amount_obj:
                    continue
                amount = float(amount_obj.amount)
                counterparty = d.get("applicant_name") or ""
                purpose = d.get("purpose") or ""
                parsed.append({
                    "booking_date": booking_date.isoformat(),
                    "amount": amount,
                    "currency": amount_obj.currency or "EUR",
                    "counterparty": counterparty,
                    "purpose": purpose,
                    "posting_text": d.get("posting_text"),
                    "category": _categorize(counterparty, purpose),
                })

            # Pass 2: one batched local-AI call for whatever the keyword
            # pass left uncategorised.
            missing = [
                {"idx": i, "counterparty": p["counterparty"], "purpose": p["purpose"], "amount": p["amount"]}
                for i, p in enumerate(parsed) if p["category"] is None
            ]
            ai_categories = _categorize_missing_via_llm(missing)
            for i, cat in ai_categories.items():
                parsed[i]["category"] = cat

            with get_conn() as conn:
                for p in parsed:
                    h = dedup_hash(p["booking_date"], p["amount"], p["counterparty"], p["purpose"])
                    try:
                        conn.execute(
                            "INSERT INTO bank_transactions "
                            "(account_id, booking_date, amount, currency, counterparty, purpose, "
                            " posting_text, category, dedup_hash) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT (account_id, dedup_hash) DO NOTHING",
                            (account_id, p["booking_date"], p["amount"], p["currency"],
                             p["counterparty"], p["purpose"], p["posting_text"], p["category"], h),
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


def recategorize_account(account_id: int) -> dict[str, Any]:
    """Backfill: re-run keyword + local-AI categorisation over rows this
    account already has but that never got a category (either synced
    before this feature shipped, or the AI call failed at the time).
    Never touches rows that already have one -- no bank access, purely
    local, safe to call repeatedly."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, counterparty, purpose, amount FROM bank_transactions "
            "WHERE account_id = ? AND category IS NULL",
            (account_id,),
        ).fetchall()
    if not rows:
        return {"ok": True, "updated": 0}

    entries = []
    keyword_hits: dict[int, str] = {}
    for r in rows:
        cat = _categorize(r["counterparty"], r["purpose"])
        if cat:
            keyword_hits[int(r["id"])] = cat
        else:
            entries.append({"idx": int(r["id"]), "counterparty": r["counterparty"],
                             "purpose": r["purpose"], "amount": float(r["amount"])})
    ai_hits = _categorize_missing_via_llm(entries)

    updated = 0
    with get_conn() as conn:
        for row_id, cat in {**keyword_hits, **ai_hits}.items():
            conn.execute("UPDATE bank_transactions SET category = ? WHERE id = ?", (cat, row_id))
            updated += 1
        conn.commit()
    return {"ok": True, "updated": updated}


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
