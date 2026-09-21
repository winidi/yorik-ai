"""The arithmetic of an invoice or quote. One place computes what the
page shows and (later) what the e-invoice XML says, so the two cannot
drift. Money is Decimal, rounded half-up per line and per VAT rate — the
way EN 16931 (BR-CO-*) wants the sums to add up.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List

CENT = Decimal("0.01")
MAX_LINES = 200


def dec(v: Any, default: str = "0") -> Decimal:
    """"1.234,56", "12,5", 3, None → Decimal. A comma is a decimal comma."""
    if isinstance(v, Decimal):
        return v
    s = str(v if v is not None else "").strip().replace("€", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        d = Decimal(s or default)
    except InvalidOperation:
        d = Decimal(default)
    return d if d.is_finite() else Decimal(default)


def _q(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def compute(lines: Any, *, small_business: bool = False, default_vat: Any = "19") -> Dict[str, Any]:
    """lines: [{text, qty, unit, unit_price, vat_percent}] → the same
    lines with `net`, and totals {net, vat, gross, vat_rows[{rate, net, vat}]}.
    A small business (§ 19 UStG) charges no VAT whatever the lines say."""
    out: List[Dict[str, Any]] = []
    per_rate: Dict[Decimal, Decimal] = {}
    for raw in (lines if isinstance(lines, list) else [])[:MAX_LINES]:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()[:2000]
        qty, price = dec(raw.get("qty"), "1"), dec(raw.get("unit_price"))
        if not text and price == 0:
            continue
        rate = Decimal("0") if small_business else dec(raw.get("vat_percent") if raw.get("vat_percent") not in (None, "") else default_vat)
        net = _q(qty * price)
        per_rate[rate] = per_rate.get(rate, Decimal("0")) + net
        out.append({"text": text, "qty": qty, "unit": str(raw.get("unit") or "").strip()[:20],
                    "unit_price": price, "vat_percent": rate, "net": net})
    vat_rows = [{"rate": r, "net": n, "vat": _q(n * r / 100)} for r, n in sorted(per_rate.items()) if r > 0 or not small_business]
    net_total = sum((l["net"] for l in out), Decimal("0"))
    vat_total = sum((v["vat"] for v in vat_rows), Decimal("0"))
    return {"lines": out, "totals": {"net": net_total, "vat": vat_total, "gross": net_total + vat_total,
                                     "vat_rows": [v for v in vat_rows if v["rate"] > 0]}}


def money(d: Decimal, country: str = "DE", currency: str = "€") -> str:
    s = f"{_q(d):,.2f}"
    if country.upper() in ("DE", "AT", "NL", "ES", "IT", "BE", "PT"):
        s = s.replace(",", " ").replace(".", ",").replace(" ", ".")
    return f"{s} {currency}"


def number(d: Decimal, country: str = "DE") -> str:
    """A quantity or a rate without needless zeros: 1, 2,5, 19."""
    s = format(d.normalize(), "f") if d == d.to_integral() else format(d.normalize(), "f")
    return s.replace(".", ",") if country.upper() in ("DE", "AT", "NL", "ES", "IT", "BE", "PT") else s
