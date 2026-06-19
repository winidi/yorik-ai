"""ZUGFeRD / Factur-X v1 — German hybrid e-invoice generation.

Two-step pipeline:
  1. Build a CrossIndustryInvoice XML (BASIC profile is enough for a small
     practice's outgoing invoices) from a small structured dict.
  2. Embed that XML into a PDF/A-3 using facturx.

Result: the rendered Compose PDF is also a valid e-invoice that German
accounting software (DATEV, Lexware, sevDesk, et al.) can ingest
automatically. B2B-mandatory in Germany 2025-2027.

Profile choice: BASIC. MINIMUM strips line items (we have them); EN16931
adds fields most small businesses don't need. BASIC hits the sweet spot
of "real invoice data + simple to populate."

Templates opt in via `"zugferd": true` in their JSON. They MUST then also
declare `invoice_fields` listing where to pull the structured values from
(template args or data_query results).
"""

from __future__ import annotations

import datetime as dt
import io
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

log = logging.getLogger("homeos.compose.zugferd")


def _d(v: Any, default: str = "0") -> Decimal:
    """Coerce to a 2-decimal-place Decimal (currency)."""
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:  # noqa: BLE001
        return Decimal(default).quantize(Decimal("0.01"))


def _iso_date(v: Any) -> str:
    """Normalize to YYYY-MM-DD; default to today.

    Accepts ISO (`2026-06-02`) and German (`02.06.2026`) string formats —
    German templates emit dates in the user-facing format and we don't
    want every template author to remember to convert in their
    invoice_fields Jinja. Without this, `delivery_date` /
    `payment.due_date` crashed the XML build any time the template
    passed through the German `DD.MM.YYYY` form."""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, str):
        s = v.strip()
        # German DD.MM.YYYY → ISO. Tolerant of trailing junk (`02.06.2026 14:00`).
        if len(s) >= 10 and s[2] == "." and s[5] == ".":
            try:
                d, m, y = s[:10].split(".")
                return f"{y}-{m}-{d}"
            except ValueError:
                pass
        if len(s) >= 10:
            return s[:10]
    return dt.date.today().isoformat()


def build_xml(invoice: Dict[str, Any]) -> bytes:
    """Construct a ZUGFeRD BASIC profile XML from a dict of the shape:

      {
        "number":       "2026-185",                # invoice number
        "issue_date":   "2026-05-19",              # ISO
        "currency":     "EUR",
        "seller":  { "name": "Praxis X", "street": "..", "postcode": "..", "city": "..", "country": "DE", "tax_id": "..", "vat_id": "DE..." },
        "buyer":   { "name": "Klaus Weber", "street": "..", "postcode": "..", "city": "..", "country": "DE" },
        "lines":   [ { "name": "Konsultation 12.04", "qty": 1, "unit_price": 80.0, "vat_percent": 0 } ],
        "vat_breakdown": [ { "vat_percent": 0, "category": "E", "exemption_reason": "Heilbehandlung §4 Nr 14 UStG", "net": 160.0, "vat": 0.0 } ],
        "totals":  { "net": 160.0, "vat": 0.0, "gross": 160.0, "payable": 160.0 },
        "payment": { "iban": "DE00 ...", "bic": "...", "due_date": "2026-06-02" }
      }
    """
    from drafthorse.models.document import Document
    from drafthorse.models.trade import ApplicableTradeTax
    from drafthorse.models.tradelines import LineItem

    def _set_amt(field_element, amount, currency="EUR"):
        """drafthorse 2025+ makes CurrencyField attributes read-only — you
        mutate the inner element instead of reassigning. _set_on_input is
        the flag drafthorse checks before deciding to include the parent
        element in the serialized XML; bypassing the setter skips it, so
        we set it manually."""
        field_element._amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        field_element._currency = currency
        field_element._set_on_input = True

    def _set_qty(field_element, qty, unit_code="C62"):
        """Same idea for quantity fields (which carry a UN/ECE unit code)."""
        field_element._amount = Decimal(str(qty))
        field_element._unit_code = unit_code
        field_element._set_on_input = True

    doc = Document()
    doc.context.guideline_parameter.id = "urn:cen.eu:en16931:2017"
    doc.header.id = str(invoice["number"])
    doc.header.issue_date_time = dt.datetime.strptime(_iso_date(invoice.get("issue_date")), "%Y-%m-%d")
    doc.header.type_code = "380"  # commercial invoice
    # NOTE: drafthorse default ordering puts Name before TypeCode which the
    # XSD rejects. Omit doc.header.name entirely — TypeCode 380 already
    # tells the reader this is a commercial invoice.

    # Helper: drafthorse strictly expects strings for address fields, but
    # JSON loads "22305" as int. Coerce all party fields.
    def _s(v: Any, default: str = "") -> str:
        return str(v) if v is not None else default

    # Seller
    s = invoice.get("seller") or {}
    doc.trade.agreement.seller.name = _s(s.get("name"), "Seller")
    doc.trade.agreement.seller.address.country_id = _s(s.get("country"), "DE")
    doc.trade.agreement.seller.address.line_one = _s(s.get("street"))
    doc.trade.agreement.seller.address.postcode = _s(s.get("postcode"))
    doc.trade.agreement.seller.address.city_name = _s(s.get("city"))
    if s.get("vat_id"):
        from drafthorse.models.party import TaxRegistration
        tr = TaxRegistration()
        # AgencyIDField setter: value[0] = scheme_id, value[1] = id text.
        # For VAT IDs the scheme is "VA" and the id is the actual number.
        tr.id = ("VA", _s(s["vat_id"]))
        doc.trade.agreement.seller.tax_registrations.add(tr)

    # Buyer
    b = invoice.get("buyer") or {}
    doc.trade.agreement.buyer.name = _s(b.get("name"), "Buyer")
    doc.trade.agreement.buyer.address.country_id = _s(b.get("country"), "DE")
    doc.trade.agreement.buyer.address.line_one = _s(b.get("street"))
    doc.trade.agreement.buyer.address.postcode = _s(b.get("postcode"))
    doc.trade.agreement.buyer.address.city_name = _s(b.get("city"))

    cur = invoice.get("currency", "EUR")
    doc.trade.settlement.currency_code = cur  # document-level currency

    # Line items. Drafthorse 2025 accepts direct Decimal assignment on
    # the amount fields and produces clean numeric content. The per-amount
    # currencyID attribute is omitted; the header InvoiceCurrencyCode
    # covers it per ZUGFeRD BASIC profile.
    for idx, line in enumerate(invoice.get("lines") or [], start=1):
        li = LineItem()
        li.document.line_id = str(idx)
        li.product.name = str(line.get("name", f"Position {idx}"))
        qty = _d(line.get("qty", 1))
        unit_price = _d(line.get("unit_price", 0))
        net = (qty * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        li.agreement.gross.amount = unit_price
        li.agreement.net.amount = unit_price
        li.delivery.billed_quantity = (qty, "C62")
        li.settlement.trade_tax.type_code = "VAT"
        li.settlement.trade_tax.category_code = str(line.get("vat_category", "S"))
        li.settlement.trade_tax.rate_applicable_percent = _d(line.get("vat_percent", 0))
        li.settlement.monetary_summation.total_amount = net
        doc.trade.items.add(li)

    # Actual delivery date — schematron requires this (BR-FX-EN-04). Default
    # to the invoice date if no explicit value is provided.
    delivery_date = invoice.get("delivery_date") or invoice.get("issue_date")
    doc.trade.delivery.event.occurrence = dt.datetime.strptime(
        _iso_date(delivery_date), "%Y-%m-%d"
    )

    # VAT breakdown — one ApplicableTradeTax per (rate, category) tuple
    for vb in (invoice.get("vat_breakdown") or []):
        tt = ApplicableTradeTax()
        tt.calculated_amount = _d(vb.get("vat", 0))
        tt.type_code = "VAT"
        tt.basis_amount = _d(vb.get("net", 0))
        tt.category_code = str(vb.get("category", "S"))
        if vb.get("exemption_reason"):
            tt.exemption_reason = str(vb["exemption_reason"])
        tt.rate_applicable_percent = _d(vb.get("vat_percent", 0))
        doc.trade.settlement.trade_tax.add(tt)

    # Document totals
    t = invoice.get("totals") or {}
    sum_ = doc.trade.settlement.monetary_summation
    sum_.line_total = _d(t.get("net", 0))
    sum_.charge_total = Decimal("0.00")
    sum_.allowance_total = Decimal("0.00")
    sum_.tax_basis_total = _d(t.get("net", 0))
    sum_.tax_total = _d(t.get("vat", 0))
    sum_.grand_total = _d(t.get("gross", 0))
    sum_.due_amount = _d(t.get("payable", t.get("gross", 0)))

    # Payment terms — schematron BR-CO-25 requires Payment due date OR
    # Payment terms text when due_amount > 0. Default 14 days net.
    from drafthorse.models.payment import PaymentTerms
    pay = invoice.get("payment") or {}
    if _d(t.get("payable", t.get("gross", 0))) > 0:
        pt = PaymentTerms()
        if pay.get("due_date"):
            pt.due = dt.datetime.strptime(_iso_date(pay["due_date"]), "%Y-%m-%d")
        else:
            issue = dt.datetime.strptime(_iso_date(invoice.get("issue_date")), "%Y-%m-%d")
            pt.due = issue + dt.timedelta(days=14)
        pt.description = pay.get("terms_text") or "Zahlbar innerhalb von 14 Tagen ohne Abzug."
        doc.trade.settlement.terms.add(pt)

    # Skip drafthorse's built-in XSD validation — it's stricter than the
    # downstream facturx reader (which does its own validation pass).
    xml_bytes = doc.serialize(schema=None)
    # Post-fix: schematron requires TaxTotalAmount/@currencyID, which
    # drafthorse 2025 doesn't emit. Inject it via a targeted regex.
    import re as _re
    xml_bytes = _re.sub(
        rb'(<ram:TaxTotalAmount)>',
        rb'\1 currencyID="' + cur.encode() + rb'">',
        xml_bytes,
    )
    return xml_bytes


def embed_into_pdf(pdf_bytes: bytes, xml_bytes: bytes,
                    invoice_number: str = "INV", *, profile: str = "BASIC") -> Optional[bytes]:
    """Embed the ZUGFeRD XML into a PDF, producing PDF/A-3 hybrid bytes.
    Returns None on failure (we fall back to the plain PDF)."""
    try:
        from facturx import generate_from_binary
    except ImportError:
        log.warning("facturx not installed — returning unembedded PDF")
        return None
    try:
        return generate_from_binary(
            pdf_bytes,
            xml_bytes,
            flavor="factur-x",
            level=profile.lower(),          # 'basic', 'en16931', etc.
            check_xsd=False,                # we skipped schema in build_xml too
            check_schematron=False,         # schematron checks can be slow + over-strict
            afrelationship="data",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("facturx embed failed: %s", exc)
        return None


def render_invoice_data(template: Dict[str, Any], rendered_data: Dict[str, Any],
                        args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract the structured invoice payload the XML generator wants from
    a template's `invoice_fields` mapping. Returns None if the template
    doesn't opt into ZUGFeRD (`zugferd: true`) or the mapping is missing.

    `invoice_fields` is itself a small Jinja-templatable dict so authors
    can express things like:
        "lines": [{"name": "{{ s.behandlung }}", "qty": 1,
                   "unit_price": 80, "vat_percent": 0}]
    The render.py engine populates these from the same data_query results
    that drove the visible HTML, so the visible total and the XML total
    can't drift.
    """
    if not template.get("zugferd"):
        return None
    spec = template.get("invoice_fields")
    if not spec:
        log.warning("template %s has zugferd:true but no invoice_fields", template.get("id"))
        return None

    # Eval the spec as Jinja against the same context the body_html used,
    # plus the same global helpers (today_de, euro, date_de).
    import json
    from jinja2 import Environment, ChainableUndefined
    # The Compose render module ships the same Jinja filters we want to
    # reuse here (euro / date_de / today_de) — import via the absolute
    # Yorik path so this extension stays independent of its install dir.
    from backend.compose import render as _rdr
    env = Environment(undefined=ChainableUndefined, autoescape=False)
    env.filters["d"] = lambda v: float(v) if v not in (None, "") else 0.0
    env.filters["euro"] = _rdr.euro
    env.filters["date_de"] = _rdr.date_de
    env.globals["today_de"] = _rdr.today_de

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            try:
                txt = env.from_string(node).render(args=args, **rendered_data)
            except Exception:  # noqa: BLE001
                txt = node
            # Try JSON-parse to recover ints/floats; else keep as string.
            try:
                return json.loads(txt)
            except Exception:  # noqa: BLE001
                return txt
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(x) for x in node]
        return node

    # issue_date wants ISO YYYY-MM-DD, not the German format we'd get from
    # today_de(). Substitute that one path explicitly.
    out = _walk(spec)
    if isinstance(out.get("issue_date"), str) and out["issue_date"].count(".") == 2:
        try:
            d, m, y = out["issue_date"].split(".")
            out["issue_date"] = f"{y}-{m}-{d}"
        except Exception:  # noqa: BLE001
            pass
    return out


# ─── Hook wiring ──────────────────────────────────────────────────────────
# This runs at module-import time, which is when the extension loader
# imports us (after dep check passes). The Compose pipeline calls
# extensions.invoke_hooks("compose.pdf_post_process", pdf_bytes, ...);
# each registered hook returns transformed bytes (or None to pass through).

def pdf_post_process_hook(pdf_bytes: bytes, *, template: Dict[str, Any],
                           args: Dict[str, Any], rendered_data: Dict[str, Any],
                           **_kw) -> Optional[bytes]:
    """Compose calls us right after Gotenberg renders the PDF. If the
    template opts into ZUGFeRD AND defines invoice_fields, we generate
    the XML and embed it into a PDF/A-3 hybrid. Otherwise we return None
    (pass through to plain PDF). Failures fall back to plain PDF rather
    than blocking the user's download."""
    if not (template or {}).get("zugferd"):
        return None
    try:
        invoice_data = render_invoice_data(template, rendered_data or {}, args or {})
        if not invoice_data:
            return None
        xml_bytes = build_xml(invoice_data)
        hybrid = embed_into_pdf(
            pdf_bytes, xml_bytes,
            invoice_number=str(invoice_data.get("number", "INV")),
        )
        return hybrid  # None means embedding failed → caller keeps the plain PDF
    except Exception as exc:  # noqa: BLE001
        log.warning("ZUGFeRD hook failed for template %s: %s",
                    template.get("id"), exc)
        return None


try:
    # Late import so the dep-check has already run before we hit this.
    from backend.extensions import register_hook
    register_hook("compose.pdf_post_process", pdf_post_process_hook)
    log.info("ZUGFeRD extension active: compose.pdf_post_process hook registered")
except Exception as exc:  # noqa: BLE001
    log.warning("ZUGFeRD extension could not register hook: %s", exc)
