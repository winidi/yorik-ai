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


# ── what an invoice must say (§ 14 UStG) ────────────────────────────────

def split_address(lines: Any) -> Dict[str, str]:
    """["Lindenallee 4", "12345 Beispielstadt"] → street, postcode, city. The
    e-invoice wants them apart; a person types them as lines."""
    import re
    lines = [str(x).strip() for x in (lines or []) if str(x).strip()]
    out = {"street": "", "postcode": "", "city": "", "country": ""}
    for i in range(len(lines) - 1, -1, -1):
        m = re.match(r"^(?:[A-Z]{1,2}-)?(\d{4,5})\s+(.+)$", lines[i])
        if m:
            out["postcode"], out["city"] = m.group(1), m.group(2)
            out["street"] = ", ".join(lines[:i])
            rest = lines[i + 1:]
            out["country"] = rest[0] if rest else ""
            return out
    out["street"] = ", ".join(lines)
    return out


def missing(kind: str, letterhead: Dict[str, Any], recipient: Dict[str, Any], content: Dict[str, Any]) -> List[str]:
    """What still stands between a draft and a valid document, in the
    person's words. A quote needs little; an invoice what § 14 UStG asks
    for (the number and the date come with finalising)."""
    out: List[str] = []
    calc = compute(content.get("lines"), small_business=bool(letterhead.get("small_business")))
    if not str(recipient.get("name") or "").strip():
        out.append("Kunde (Name)")
    if not calc["lines"]:
        out.append("mindestens eine Position")
    if kind != "invoice":
        return out
    addr = split_address(recipient.get("address_lines"))
    if not (addr["street"] and addr["postcode"] and addr["city"]):
        out.append("Anschrift des Kunden (Straße, PLZ, Ort)")
    if any(not l["text"] for l in calc["lines"]):
        out.append("Beschreibung bei jeder Position")
    if not (content.get("service_from") or content.get("service_to")):
        out.append("Leistungsdatum oder -zeitraum")
    if not (letterhead.get("sender_name") or letterhead.get("business_name")):
        out.append("dein Name im Briefpapier")
    if not (letterhead.get("street") and letterhead.get("postcode") and letterhead.get("city")):
        out.append("deine Anschrift im Briefpapier")
    if not (letterhead.get("vat_id") or letterhead.get("tax_id")):
        out.append("Steuernummer oder USt-IdNr. im Briefpapier")
    return out


def einvoice_payload(letterhead: Dict[str, Any], recipient: Dict[str, Any], content: Dict[str, Any], *,
                     number: str, issue_date: str, due_date: str) -> Dict[str, Any]:
    """The same figures the page shows, in the shape the XML builder
    (extensions/zugferd) takes. One computation feeds both."""
    small = bool(letterhead.get("small_business"))
    calc = compute(content.get("lines"), small_business=small, default_vat=content.get("vat_percent", "19"))
    t = calc["totals"]
    buyer = split_address(recipient.get("address_lines"))
    reason = str(letterhead.get("small_business_text") or "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.")

    def category(rate: Decimal) -> str:
        return "E" if rate == 0 else "S"

    rows = [{"vat_percent": str(v["rate"]), "category": "S", "net": str(v["net"]), "vat": str(v["vat"])} for v in t["vat_rows"]]
    zero_net = sum((l["net"] for l in calc["lines"] if l["vat_percent"] == 0), Decimal("0"))
    if zero_net or not rows:
        rows.append({"vat_percent": "0", "category": "E", "net": str(zero_net), "vat": "0.00", "exemption_reason": reason})
    return {
        "number": number, "issue_date": issue_date, "currency": "EUR",
        "seller": {"name": letterhead.get("business_name") or letterhead.get("sender_name"), "street": letterhead.get("street"),
                   "postcode": letterhead.get("postcode"), "city": letterhead.get("city"), "country": letterhead.get("country") or "DE",
                   "vat_id": letterhead.get("vat_id") or None, "tax_id": None if letterhead.get("vat_id") else (letterhead.get("tax_id") or None)},
        "buyer": {"name": recipient.get("name"), "street": buyer["street"], "postcode": buyer["postcode"], "city": buyer["city"],
                  "country": (buyer["country"] if len(buyer["country"]) == 2 else "") or letterhead.get("country") or "DE"},
        "lines": [{"name": l["text"].replace("\n", " — "), "qty": str(l["qty"]), "unit_price": str(l["unit_price"]),
                   "vat_percent": str(l["vat_percent"]), "vat_category": category(l["vat_percent"])} for l in calc["lines"]],
        "vat_breakdown": rows,
        "totals": {"net": str(t["net"]), "vat": str(t["vat"]), "gross": str(t["gross"]), "payable": str(t["gross"])},
        "delivery_date": content.get("service_to") or content.get("service_from") or issue_date,
        "payment": {"iban": letterhead.get("iban"), "bic": letterhead.get("bic"), "due_date": due_date,
                    "terms_text": f"Zahlbar bis {due_date[8:10]}.{due_date[5:7]}.{due_date[0:4]} ohne Abzug."},
    }

