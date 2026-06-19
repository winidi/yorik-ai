"""German banking — read-only via FinTS / HBCI.

Works with any German bank that supports FinTS PIN/TAN (most do). Read-only
operations only — Yorik does NOT initiate transfers, by design. The
permission model is: family/business sees balances + recent transactions for
budgeting; only the user, in their bank's own app, can move money.

Operations:
  {op: "accounts"}                                  → list configured accounts
  {op: "balance", account_iban?}                    → current balance per account
  {op: "transactions", account_iban?, days?}        → recent transactions (default 30 days)

Credentials (stored encrypted in Yorik's credential_store):
  bank_url    — your bank's FinTS server URL (look up via https://www.hbci-zka.de/)
  blz         — 8-digit Bankleitzahl (bank code)
  user_id     — your online-banking username
  pin         — your online-banking PIN
  product_id  — optional: registered product id (some banks require)

Two-factor handling: if your bank uses pushTAN/photoTAN, the first call may
trigger a TAN prompt in your banking app — interactive flows aren't supported
in v1. Use a TAN-free product (some banks issue these for read-only API
access) or accept that initial sync needs an in-app confirmation.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from . import ConnectorSpec, register
from .. import credential_store

log = logging.getLogger("homeos.connectors.banking_fints")
CONNECTOR_NAME = "banking-fints"

try:
    from fints.client import FinTS3PinTanClient  # type: ignore
    _AVAILABLE = True
    _AVAIL_ERROR = None
except ImportError as exc:
    FinTS3PinTanClient = None  # type: ignore
    _AVAILABLE = False
    _AVAIL_ERROR = str(exc)


def _client():
    c = credential_store.get(CONNECTOR_NAME)
    if not c:
        return None, {"ok": False, "needs_install": True,
                      "error": "banking-fints not configured — run install_connector to enter your bank credentials."}
    if not _AVAILABLE:
        return None, {"ok": False, "error": f"python-fints not installed: {_AVAIL_ERROR}"}
    client = FinTS3PinTanClient(
        c["blz"],
        c["user_id"],
        c["pin"],
        c["bank_url"],
        product_id=c.get("product_id") or None,
    )
    return client, c


def _serialize_balance(b) -> Dict[str, Any]:
    return {
        "amount": float(b.amount.amount) if b and b.amount else None,
        "currency": b.amount.currency if b and b.amount else None,
        "date": b.date.isoformat() if b and b.date else None,
    }


def _serialize_account(a) -> Dict[str, Any]:
    return {
        "iban": getattr(a, "iban", None),
        "account_number": getattr(a, "accountnumber", None),
        "subaccount": getattr(a, "subaccount", None),
        "type": str(getattr(a, "type", None)) if getattr(a, "type", None) is not None else None,
    }


def _serialize_tx(t) -> Dict[str, Any]:
    data = t.data if hasattr(t, "data") else {}
    return {
        "date":          data.get("date").isoformat() if data.get("date") else None,
        "amount":        float(data["amount"].amount) if data.get("amount") else None,
        "currency":      data["amount"].currency if data.get("amount") else None,
        "applicant":     data.get("applicant_name"),
        "purpose":       data.get("purpose"),
        "posting_text":  data.get("posting_text"),
        "transaction_code": data.get("transaction_code"),
    }


def banking_fints(op: str, **kw) -> Dict[str, Any]:
    client_pair = _client()
    if isinstance(client_pair, tuple) and client_pair[0] is None:
        return client_pair[1]
    client, creds = client_pair
    op = (op or "").lower().strip()
    try:
        with client:
            accounts = client.get_sepa_accounts()
            if op == "accounts":
                return {"accounts": [_serialize_account(a) for a in accounts]}

            # filter to a single IBAN if requested
            iban_filter = kw.get("account_iban")
            accs = [a for a in accounts if not iban_filter or a.iban == iban_filter]
            if not accs:
                return {"ok": False, "error": f"no account found for IBAN {iban_filter!r}"}

            if op == "balance":
                balances = []
                for a in accs:
                    bal = client.get_balance(a)
                    balances.append({"account": _serialize_account(a), "balance": _serialize_balance(bal)})
                return {"balances": balances}

            if op == "transactions":
                days = int(kw.get("days") or 30)
                end = date.today()
                start = end - timedelta(days=days)
                out: List[Dict[str, Any]] = []
                for a in accs:
                    txs = client.get_transactions(a, start, end)
                    out.append({
                        "account": _serialize_account(a),
                        "from_date": start.isoformat(),
                        "to_date": end.isoformat(),
                        "transactions": [_serialize_tx(t) for t in txs],
                    })
                return {"results": out}

            return {"ok": False, "error": f"unknown op '{op}'. Try: accounts, balance, transactions."}
    except Exception as exc:  # noqa: BLE001
        log.warning("banking-fints op=%s failed: %s", op, exc, exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


register(ConnectorSpec(
    name=CONNECTOR_NAME,
    description=(
        "READ-ONLY access to a German bank account via FinTS / HBCI. Useful for "
        "asking 'what did we spend on groceries this month' or 'show me my balance'. "
        "Cannot initiate transfers — moving money still requires your bank's own app. "
        "Operations: {op: 'accounts'}, {op: 'balance'}, {op: 'transactions', days: 30}."
    ),
    params_schema={
        "type": "object",
        "required": ["op"],
        "properties": {
            "op":           {"type": "string", "enum": ["accounts", "balance", "transactions"]},
            "account_iban": {"type": "string", "description": "Filter to one account. Omit for all."},
            "days":         {"type": "integer", "minimum": 1, "maximum": 90, "default": 30},
        },
    },
    invoke=banking_fints,
    requires_auth=True,
    install_hint=(
        "Find your bank's FinTS URL at https://www.hbci-zka.de/ — search by bank name. "
        "You'll need your BLZ (8 digits), online-banking username, and PIN. The first "
        "call may trigger a TAN prompt in your banking app."
    ),
    backend="builtin",
    version="1.0",
    tags=["banking", "germany", "fints", "hbci", "read-only", "auth"],
    credentials_schema={
        "type": "object",
        "required": ["bank_url", "blz", "user_id", "pin"],
        "properties": {
            "bank_url":   {"type": "string", "title": "FinTS server URL", "description": "Look up at hbci-zka.de — e.g. https://hbci-pintan-pq.gad.de/cgi-bin/hbciservlet"},
            "blz":        {"type": "string", "title": "BLZ", "description": "8-digit German bank code"},
            "user_id":    {"type": "string", "title": "Online-banking username"},
            "pin":        {"type": "string", "title": "PIN", "format": "password"},
            "product_id": {"type": "string", "title": "Product ID (optional)", "description": "Some banks require a registered product id."},
        },
    },
))
