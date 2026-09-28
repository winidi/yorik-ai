"""Payments that come back: subscriptions, servers, rent — found by code
in the bank bookings, so "was geben wir jeden Monat für Computerkram
aus?" starts from every regular payee instead of the ones the model
guesses (chat test #7, 2026-09-28: it asked for netcup, claude, aws,
google, microsoft and never for Hetzner, which is paid every month).

A payee is recurring when it was paid at least twice with a steady gap:
about a month, a quarter or a year apart. Round-ups to savings and money
coming in are left out. The monthly equivalent is computed here.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from statistics import median
from typing import Any, Dict, List, Optional

RHYTHMS = (("monthly", 24, 38, 1), ("quarterly", 80, 100, 3), ("yearly", 350, 380, 12))


def payee_key(counterparty: str) -> str:
    """"VISA HETZNER ONLINE GMBH   " → "HETZNER ONLINE GMBH"; card prefixes,
    reference numbers and spacing do not make another payee."""
    s = (counterparty or "").upper().strip()
    s = re.sub(r"^(VISA|MASTERCARD|MC|EC|GIROCARD|MAESTRO|SEPA|LASTSCHRIFT)\s+", "", s)
    s = re.sub(r"\b\d{5,}\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def find(ctx, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    from . import spaces
    from .database import get_conn
    from .skills.show_transactions.skill import is_roundup
    until = until or date.today().isoformat()
    since = since or (date.fromisoformat(until) - timedelta(days=400)).isoformat()
    frag, params = spaces.row_filter(str(ctx.user_id), getattr(ctx, "role", None), "bank_accounts", table_alias="a")
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT t.booking_date, t.amount, t.counterparty, t.purpose, t.category "
            f"FROM bank_transactions t JOIN bank_accounts a ON a.id = t.account_id WHERE {frag} "
            "AND t.amount < 0 AND t.booking_date >= ? AND t.booking_date <= ? ORDER BY t.booking_date",
            (*params, since, until)).fetchall()]
    from .payments import _records_start
    first = _records_start(ctx)              # every booking counts, money in too
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        if is_roundup(r):
            continue
        key = payee_key(r["counterparty"])
        if key:
            groups.setdefault(key, []).append(r)
    out = []
    for key, items in groups.items():
        cents = [round(-float(i["amount"]) * 100) for i in items]
        # The same amount coming back is what makes a subscription;
        # groceries differ every time (Penny 17 €, 28 €, 34 €, 53 €).
        same = [i for n, i in enumerate(items)
                if any(m != n and abs(cents[m] - cents[n]) <= max(5, cents[n] * 0.05) for m in range(len(items)))]
        if len(same) < 2:
            continue
        days = [date.fromisoformat(i["booking_date"][:10]) for i in same]
        gaps = [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 3]
        if not gaps:
            continue
        rhythm = next(((name, months) for name, lo, hi, months in RHYTHMS if lo <= median(gaps) <= hi), None)
        if not rhythm:
            continue
        regular = round(-float(same[-1]["amount"]) * 100)          # the latest of the recurring amounts
        out.append({
            "payee": key.title(),
            "rhythm": rhythm[0],
            "regular_amount": _eur(regular),
            "per_month": _eur(round(regular / rhythm[1])),
            "per_month_cents": round(regular / rhythm[1]),
            "last_booked": items[-1]["booking_date"][:10],
            "last_amount": _eur(cents[-1]),
            "other_amounts": sorted({_eur(c) for c in cents if abs(c - regular) > max(5, regular * 0.05)})[:4],
            "times_booked": len(items),
            "category": items[-1].get("category") or "",
        })
    out.sort(key=lambda p: -p["per_month_cents"])
    total = sum(p["per_month_cents"] for p in out)
    return {"from": since, "to": until, "bank_records_from": first,
            "recurring": [{k: v for k, v in p.items() if k != "per_month_cents"} for p in out],
            "per_month_total": _eur(total)}


def _eur(cents: int) -> str:
    s = f"{abs(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"
