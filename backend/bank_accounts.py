"""Finance app — bank account CRUD + FinTS connection testing.

One row per connected account (backend/connectors/banking_fints.py's
own single global credential slot doesn't fit "Dirk has Sparkasse +
ING privately, plus a joint account with Beate" — this follows the
email_accounts pattern instead: one row per account, its own
credential_store key, standard visibility via backend/spaces.py).

The PIN never passes through chat or an LLM. It only ever travels
from the user's own browser straight to POST /accounts, exactly like
an email account's password — see docs/plans/2026-09-25-finanzen.md.

Read-only by design: nothing here builds a transfer. The FinTS client
is only ever used for get_sepa_accounts / get_balance / get_transactions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import credential_store, spaces
from .auth_sessions import current_user
from .database import get_conn

# Bank name -> FinTS PIN/TAN URL + BLZ, so "Konto verbinden" can offer a
# search instead of making the user hunt down a FinTS server URL by
# hand (real UX complaint, 2026-09-25) -- generated once from the
# actively-maintained hbci4java project (used by Jameica/Hibiscus and
# many other German FinTS clients), NOT bundled with python-fints
# itself, which ships no institute database at all. Re-generate with:
#   curl -sSL https://raw.githubusercontent.com/hbci4j/hbci4java/master/src/main/resources/blz.properties
# and re-run the one-off parse (see docs/plans/2026-09-25-finanzen.md).
_INSTITUTES_PATH = Path(__file__).parent / "fints_institutes.json"
_institutes_cache: Optional[list[dict[str, str]]] = None


def _load_institutes() -> list[dict[str, str]]:
    global _institutes_cache
    if _institutes_cache is None:
        try:
            _institutes_cache = json.loads(_INSTITUTES_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load fints_institutes.json: %s", exc)
            _institutes_cache = []
    return _institutes_cache


def search_institutes(query: str, limit: int = 20) -> list[dict[str, str]]:
    q = (query or "").strip().lower()
    if len(q) < 2:
        return []
    out = []
    for inst in _load_institutes():
        if q in inst["name"].lower() or q in inst["city"].lower() or q in inst["blz"]:
            out.append(inst)
            if len(out) >= limit:
                break
    return out

log = logging.getLogger("yorik.bank_accounts")

try:
    from fints.client import FinTS3PinTanClient  # type: ignore
    _AVAILABLE = True
    _AVAIL_ERROR = None
except ImportError as exc:  # pragma: no cover - dependency always installed today
    FinTS3PinTanClient = None  # type: ignore
    _AVAILABLE = False
    _AVAIL_ERROR = str(exc)

# python-fints 4+ raises TypeError if product_id is falsy at all — no
# bundled placeholder anymore (found 2026-09-25; older guidance about a
# library-shipped generic test id no longer holds for the installed
# v5.0.0). This is a widely-used community placeholder while a real DK
# registration is pending, NOT an official value — the DK does not
# appear to validate it live against a bank's own FinTS server, but
# that is not guaranteed for every bank. Swap for the real id the
# moment it arrives (10-15 business days after submitting the form).
_FALLBACK_TEST_PRODUCT_ID = "999999999999"


def _test_connection(bank_url: str, blz: str, login_name: str, pin: str,
                     product_id: Optional[str]) -> dict[str, Any]:
    """Blocking — call via asyncio.to_thread. Opens one FinTS dialog and
    lists SEPA accounts; raises whatever python-fints raises on a bad
    PIN/BLZ/URL or an unhandled TAN challenge (pushTAN/photoTAN on the
    very first login isn't handled interactively yet — same limitation
    banking_fints.py already documented)."""
    if not _AVAILABLE:
        return {"ok": False, "error": f"python-fints not installed: {_AVAIL_ERROR}"}
    try:
        client = FinTS3PinTanClient(blz, login_name, pin, bank_url,
                                     product_id=product_id or _FALLBACK_TEST_PRODUCT_ID)
        with client:
            accounts = client.get_sepa_accounts()
        return {"ok": True, "accounts": [
            {"iban": getattr(a, "iban", None), "account_number": getattr(a, "accountnumber", None)}
            for a in accounts
        ]}
    except Exception as exc:  # noqa: BLE001
        log.warning("bank connection test failed for blz=%s: %s", blz, exc)
        return {"ok": False, "error": _human_bank_error(exc)}


def _human_bank_error(exc: Exception) -> str:
    """What went wrong at the bank, in words (the Finance app is German).
    The raw exception is in the log line above."""
    t = f"{type(exc).__name__}: {exc}".lower()
    if "tan" in t and any(k in t for k in ("need", "requir", "erforder", "challenge", "pushtan", "phototan")):
        return ("Die Bank verlangt beim ersten Anmelden eine TAN (pushTAN, photoTAN). "
                "Das kann Yorik noch nicht. Bitte vorerst ein Konto ohne TAN-Pflicht beim Abruf verwenden.")
    if any(k in t for k in ("pin", "9910", "9931", "9942", "login", "anmeld", "zugang", "credential")):
        return "Die Bank hat Zugangsnummer oder PIN abgelehnt. Bitte beides noch einmal prüfen."
    if any(k in t for k in ("timeout", "timed out", "connection", "resolve", "name or service", "ssl")):
        return "Die Bank war nicht erreichbar. Bitte die Bank in der Suche neu auswählen oder später noch einmal versuchen."
    return "Die Verbindung zur Bank hat nicht geklappt. Bitte die Angaben prüfen und es noch einmal versuchen."


FOCUS_CATEGORIES_DEFAULT = ["Lebensmittel", "Verträge & Abos"]
FOCUS_CATEGORIES_MAX = 4


def _focus_categories_key(user_id: str) -> str:
    return f"finance_focus_categories_{user_id}"


def dedup_hash(booking_date: str, amount: float, counterparty: str, purpose: str) -> str:
    """FinTS gives no stable transaction id. Hash the fields that make a
    booking unique in practice so a re-sync of an overlapping date
    range doesn't insert the same row twice."""
    raw = f"{booking_date}|{amount}|{(counterparty or '').strip()}|{(purpose or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d.pop("credential_key", None)  # never leaves the server
    return d


class AccountCreate(BaseModel):
    display_name: str
    bank_url: str
    blz: str
    login_name: str
    pin: str
    product_id: Optional[str] = None
    shared: bool = False  # True -> visible to everyone in the household's Finance space


class AccountUpdate(BaseModel):
    display_name: Optional[str] = None
    enabled: Optional[bool] = None
    pin: Optional[str] = None  # if set, replaces the stored PIN


router = APIRouter(prefix="/api/bank", tags=["bank"])


@router.get("/institutes")
def list_institutes(q: str = "", user: dict = Depends(current_user)) -> list[dict[str, str]]:
    """Bank search for the 'Konto verbinden' form — name/city/BLZ match
    against the bundled institute list, so the user picks a bank
    instead of hand-typing a FinTS server URL. Needs >= 2 characters;
    empty otherwise (an empty query would just be the whole list)."""
    return search_institutes(q)


def _finance_space_id() -> Optional[int]:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM spaces WHERE slug = 'finance' LIMIT 1").fetchone()
    return int(row["id"]) if row else None


@router.get("/accounts")
def list_accounts(user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    frag, params = spaces.row_filter(user["id"], user.get("role"), "bank_accounts")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM bank_accounts WHERE {frag} ORDER BY id", params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/accounts", status_code=201)
async def create_account(body: AccountCreate, user: dict = Depends(current_user)) -> dict[str, Any]:
    test = await asyncio.to_thread(_test_connection, body.bank_url, body.blz, body.login_name,
                                    body.pin, body.product_id)
    if not test["ok"]:
        raise HTTPException(400, test["error"])

    cred_key = f"bank:{secrets.token_urlsafe(12)}"
    credential_store.put(cred_key, {"pin": body.pin})

    space_id = _finance_space_id() if body.shared else None
    iban = (test.get("accounts") or [{}])[0].get("iban")

    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO bank_accounts "
                "(owner_user_id, space_id, display_name, bank_url, blz, login_name, iban, "
                " product_id, credential_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user["id"], space_id, body.display_name, body.bank_url, body.blz,
                 body.login_name, iban, body.product_id, cred_key),
            )
            aid = cur.lastrowid
        except Exception as exc:  # noqa: BLE001
            credential_store.delete(cred_key)
            raise HTTPException(500, f"DB insert failed: {exc}")
        conn.commit()
        row = conn.execute("SELECT * FROM bank_accounts WHERE id=?", (aid,)).fetchone()

    # First sync right away so the account isn't empty until the next
    # scheduled tick.
    from . import bank_sync
    try:
        await asyncio.to_thread(bank_sync.sync_account, aid)
    except Exception:  # noqa: BLE001
        pass  # account is saved either way; the scheduler will retry

    return _row_to_dict(row)


@router.patch("/accounts/{account_id}")
def update_account(account_id: int, body: AccountUpdate, user: dict = Depends(current_user)) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bank_accounts WHERE id=?", (account_id,)).fetchone()
        if not row or str(row["owner_user_id"]) != str(user["id"]):
            raise HTTPException(404, "account not found")
        if body.pin is not None:
            credential_store.put(row["credential_key"], {"pin": body.pin})
        fields, params = [], []
        if body.display_name is not None:
            fields.append("display_name = ?"); params.append(body.display_name)
        if body.enabled is not None:
            fields.append("enabled = ?"); params.append(1 if body.enabled else 0)
        if fields:
            params.append(account_id)
            conn.execute(f"UPDATE bank_accounts SET {', '.join(fields)} WHERE id = ?", params)
            conn.commit()
        row = conn.execute("SELECT * FROM bank_accounts WHERE id=?", (account_id,)).fetchone()
    return _row_to_dict(row)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bank_accounts WHERE id=?", (account_id,)).fetchone()
        if not row or str(row["owner_user_id"]) != str(user["id"]):
            raise HTTPException(404, "account not found")
        credential_store.delete(row["credential_key"])
        conn.execute("DELETE FROM bank_accounts WHERE id=?", (account_id,))
        conn.commit()


@router.post("/accounts/{account_id}/sync")
async def sync_now(account_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT owner_user_id FROM bank_accounts WHERE id=?", (account_id,)).fetchone()
        if not row or str(row["owner_user_id"]) != str(user["id"]):
            raise HTTPException(404, "account not found")
    from . import bank_sync
    result = await asyncio.to_thread(bank_sync.sync_account, account_id)
    return result


@router.get("/transactions")
def list_transactions(days: int = 30, account_id: Optional[int] = None,
                      category: Optional[str] = None,
                      user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    frag, params = spaces.row_filter(user["id"], user.get("role"), "bank_accounts",
                                      table_alias="a")
    q = (
        "SELECT t.* FROM bank_transactions t JOIN bank_accounts a ON a.id = t.account_id "
        f"WHERE {frag} AND t.booking_date >= ?"
    )
    params = list(params) + [since]
    if account_id is not None:
        q += " AND t.account_id = ?"
        params.append(account_id)
    if category is not None:
        q += " AND t.category = ?"
        params.append(category)
    q += " ORDER BY t.booking_date DESC, t.id DESC"
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    # amount is Postgres NUMERIC -> a Decimal in Python; left as-is it
    # serialises to a JSON string ("-42.10"), which a JS client adds
    # with `+` as text concatenation instead of arithmetic. A float is
    # precise enough for a currency amount and round-trips as a real
    # JSON number.
    out = []
    for r in rows:
        d = dict(r)
        d["amount"] = float(d["amount"])
        out.append(d)
    return out


def _normalize_counterparty(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def detect_recurring(rows: list[dict[str, Any]], months: int = 6) -> list[dict[str, Any]]:
    """Wiederkehrende Zahlungen — computed on the fly from bank_transactions,
    no schema change and no ML: group outgoing transactions by (account,
    normalized counterparty), keep groups seen in >= 2 distinct calendar
    months with a stable amount (+/-5%, floor 1 EUR so cent-level FX
    rounding doesn't disqualify a real subscription). Misses subscriptions
    whose merchant string changes between charges (e.g. a payment
    processor appending a random reference) — acceptable for v1, a real
    "same merchant, always slightly different text" case would need
    fuzzy matching we don't have data yet to tune.
    """
    from collections import defaultdict
    from datetime import date as _date, timedelta as _timedelta

    groups: dict[tuple[Any, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["amount"] >= 0:
            continue
        key = (r["account_id"], _normalize_counterparty(r["counterparty"] or r["purpose"] or ""))
        if not key[1]:
            continue
        groups[key].append(r)

    out = []
    for (account_id, _norm), items in groups.items():
        items.sort(key=lambda r: r["booking_date"])
        months_seen = {r["booking_date"][:7] for r in items}
        if len(months_seen) < 2:
            continue
        amounts = [abs(r["amount"]) for r in items]
        mean_amt = sum(amounts) / len(amounts)
        tolerance = max(mean_amt * 0.05, 1.0)
        if max(abs(a - mean_amt) for a in amounts) > tolerance:
            continue

        dates = [_date.fromisoformat(r["booking_date"]) for r in items]
        diffs = [(b - a).days for a, b in zip(dates, dates[1:])]
        avg_interval = sum(diffs) / len(diffs) if diffs else None
        last_date = dates[-1]
        next_expected = (last_date + _timedelta(days=round(avg_interval))) if avg_interval else None

        categories = [r.get("category") for r in items if r.get("category")]
        category = max(set(categories), key=categories.count) if categories else None

        out.append({
            "account_id": account_id,
            "counterparty": items[-1]["counterparty"] or items[-1]["purpose"] or "—",
            "category": category,
            "latest_amount": items[-1]["amount"],
            "avg_amount": round(-mean_amt, 2),
            "months_seen": len(months_seen),
            "last_date": last_date.isoformat(),
            "avg_interval_days": round(avg_interval) if avg_interval else None,
            "next_expected": next_expected.isoformat() if next_expected else None,
        })

    out.sort(key=lambda d: d["avg_amount"])  # most expensive (most negative) first
    return out


@router.get("/recurring")
def list_recurring(months: int = 6, account_id: Optional[int] = None,
                   user: dict = Depends(current_user)) -> list[dict[str, Any]]:
    """Verträge & Abos — see detect_recurring() for the heuristic."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=months * 31)).isoformat()
    frag, params = spaces.row_filter(user["id"], user.get("role"), "bank_accounts",
                                      table_alias="a")
    q = (
        "SELECT t.account_id, t.booking_date, t.amount, t.counterparty, t.purpose, t.category "
        "FROM bank_transactions t JOIN bank_accounts a ON a.id = t.account_id "
        f"WHERE {frag} AND t.booking_date >= ?"
    )
    params = list(params) + [since]
    if account_id is not None:
        q += " AND t.account_id = ?"
        params.append(account_id)
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    parsed = []
    for r in rows:
        d = dict(r)
        d["amount"] = float(d["amount"])
        d["booking_date"] = str(d["booking_date"])
        parsed.append(d)
    return detect_recurring(parsed, months=months)


class FocusCategoriesBody(BaseModel):
    categories: list[str]


@router.get("/focus-categories")
def get_focus_categories(user: dict = Depends(current_user)) -> dict[str, list[str]]:
    """Which categories the Übersicht dashboard's quick-stats pin — a
    per-user preference, not a real table (same app_settings pattern as
    prepare_email's staged draft)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_focus_categories_key(user["id"]),),
        ).fetchone()
    if not row:
        return {"categories": FOCUS_CATEGORIES_DEFAULT}
    try:
        cats = json.loads(row["value"])
        if isinstance(cats, list) and cats:
            return {"categories": cats}
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return {"categories": FOCUS_CATEGORIES_DEFAULT}


@router.put("/focus-categories")
def set_focus_categories(body: FocusCategoriesBody, user: dict = Depends(current_user)) -> dict[str, list[str]]:
    cats = [c.strip() for c in body.categories if c.strip()][:FOCUS_CATEGORIES_MAX]
    if not cats:
        cats = FOCUS_CATEGORIES_DEFAULT
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (_focus_categories_key(user["id"]), json.dumps(cats)),
        )
        conn.commit()
    return {"categories": cats}


@router.post("/accounts/{account_id}/recategorize")
async def recategorize(account_id: int, user: dict = Depends(current_user)) -> dict[str, Any]:
    """Re-run categorisation (keyword rules + local-AI fallback) over an
    account's existing transactions — for the backfill after this
    feature shipped, and for anyone who wants a redo after the rules
    changed. Read-only w.r.t. the bank itself; only touches our own
    bank_transactions.category column."""
    with get_conn() as conn:
        row = conn.execute("SELECT owner_user_id FROM bank_accounts WHERE id=?", (account_id,)).fetchone()
        if not row or str(row["owner_user_id"]) != str(user["id"]):
            raise HTTPException(404, "account not found")
    from . import bank_sync
    result = await asyncio.to_thread(bank_sync.recategorize_account, account_id)
    return result
