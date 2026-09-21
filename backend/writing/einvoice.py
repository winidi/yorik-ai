"""The e-invoice (ZUGFeRD / Factur-X, profile EN 16931): the XML built
from the very figures the page shows, embedded in a PDF/A-3, and checked
before anybody gets it. The libraries come with the extension "zugferd"
(Settings → Extensions); without them an invoice can still be made, but
the person is told that it is a plain PDF — nothing falls back silently.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("yorik.writing.einvoice")
LEVEL = "en16931"           # what build_xml declares (urn:cen.eu:en16931:2017)
_EXT = Path(__file__).resolve().parents[2] / "extensions" / "zugferd" / "zugferd.py"


def available() -> bool:
    importlib.invalidate_caches()        # the extension may have been installed a minute ago, into this running process
    return all(importlib.util.find_spec(m) is not None for m in ("drafthorse", "facturx")) and _EXT.exists()


def _builder():
    spec = importlib.util.spec_from_file_location("yorik_zugferd_builder", _EXT)
    mod = importlib.util.module_from_spec(spec)          # type: ignore[arg-type]
    spec.loader.exec_module(mod)                         # type: ignore[union-attr]
    return mod


def build_xml(payload: Dict[str, Any]) -> bytes:
    return _builder().build_xml(payload)


def check_xml(xml: bytes) -> List[str]:
    """Empty = valid. Two checks that really run here: the profile's
    schema (XSD), and the sums read back out of the XML and added up
    again (the arithmetic rules of EN 16931, BR-CO-10 to 16, and the
    parties an invoice needs). facturx's own schematron check wants a
    Saxon server and silently skips without one, so it is not relied on;
    the full rule set is what `scripts/validate_einvoice.sh` (Mustang)
    runs."""
    import facturx
    problems: List[str] = []
    try:
        facturx.xml_check_xsd(xml, flavor="factur-x", level=LEVEL)
    except Exception as exc:  # noqa: BLE001
        return [f"Schema: {str(exc)[:400]}"]
    return problems + check_sums(xml)


def check_sums(xml: bytes) -> List[str]:
    from decimal import Decimal
    from lxml import etree
    ns = {"rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
          "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"}
    root = etree.fromstring(xml)

    def one(path: str, node=root) -> str:
        found = node.xpath(path, namespaces=ns)
        return (found[0].text or "").strip() if found else ""

    def num(path: str, node=root) -> Decimal:
        try:
            return Decimal(one(path, node) or "0")
        except Exception:  # noqa: BLE001
            return Decimal("0")

    cent, out = Decimal("0.01"), []
    trade = "//rsm:SupplyChainTradeTransaction"
    for label, path in (("Rechnungsnummer", "//rsm:ExchangedDocument/ram:ID"), ("Rechnungsdatum", "//rsm:ExchangedDocument/ram:IssueDateTime/*"),
                        ("Name des Verkäufers", f"{trade}//ram:SellerTradeParty/ram:Name"), ("Name des Käufers", f"{trade}//ram:BuyerTradeParty/ram:Name"),
                        ("Land des Verkäufers", f"{trade}//ram:SellerTradeParty/ram:PostalTradeAddress/ram:CountryID"),
                        ("Steuernummer oder USt-IdNr. des Verkäufers", f"{trade}//ram:SellerTradeParty/ram:SpecifiedTaxRegistration/ram:ID")):
        if not one(path):
            out.append(f"{label} fehlt im XML")
    seller = f"{trade}//ram:SellerTradeParty"
    if not (one(f"{seller}/ram:ID") or one(f"{seller}/ram:GlobalID") or one(f"{seller}/ram:SpecifiedLegalOrganization/ram:ID")
            or one(f"{seller}/ram:SpecifiedTaxRegistration/ram:ID[@schemeID='VA']")):
        out.append("Verkäufer ist nicht eindeutig benannt: USt-IdNr., Verkäuferkennung oder Registernummer fehlt (BR-CO-26)")
    lines = root.xpath(f"{trade}/ram:IncludedSupplyChainTradeLineItem", namespaces=ns)
    if not lines:
        out.append("keine Position im XML")
    line_sum = Decimal("0")
    for i, li in enumerate(lines, start=1):
        qty, price = num(".//ram:BilledQuantity", li), num(".//ram:NetPriceProductTradePrice/ram:ChargeAmount", li)
        total = num(".//ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount", li)
        if abs((qty * price).quantize(cent) - total) > cent:
            out.append(f"Position {i}: Menge × Preis ergibt nicht den Zeilenbetrag")
        line_sum += total
    head = f"{trade}/ram:ApplicableHeaderTradeSettlement"
    s = f"{head}/ram:SpecifiedTradeSettlementHeaderMonetarySummation"
    line_total, basis, tax, grand, due = (num(f"{s}/ram:{n}") for n in ("LineTotalAmount", "TaxBasisTotalAmount", "TaxTotalAmount", "GrandTotalAmount", "DuePayableAmount"))
    if line_sum != line_total:
        out.append("Summe der Positionen stimmt nicht mit dem Nettobetrag überein (BR-CO-10)")
    if basis != line_total - num(f"{s}/ram:AllowanceTotalAmount") + num(f"{s}/ram:ChargeTotalAmount"):
        out.append("Steuerbasis stimmt nicht (BR-CO-13)")
    tax_rows = root.xpath(f"{head}/ram:ApplicableTradeTax", namespaces=ns)
    if not tax_rows:
        out.append("keine Steueraufschlüsselung im XML")
    vat_sum = basis_sum = Decimal("0")
    for row in tax_rows:
        b, v, rate = num("ram:BasisAmount", row), num("ram:CalculatedAmount", row), num("ram:RateApplicablePercent", row)
        if abs((b * rate / 100).quantize(cent) - v) > cent:
            out.append(f"Steuer zu {rate} % passt nicht zur Basis (BR-CO-17)")
        if one("ram:CategoryCode", row) == "E" and not one("ram:ExemptionReason", row):
            out.append("Steuerbefreiung ohne Begründung (BR-E-10)")
        vat_sum += v; basis_sum += b
    if vat_sum != tax:
        out.append("Summe der Steuerbeträge stimmt nicht (BR-CO-14)")
    if basis_sum != basis:
        out.append("Steueraufschlüsselung deckt den Nettobetrag nicht (BR-S-08)")
    if grand != basis + tax:
        out.append("Bruttobetrag ist nicht Netto plus Steuer (BR-CO-15)")
    if due != grand - num(f"{s}/ram:TotalPrepaidAmount"):
        out.append("Zahlbetrag stimmt nicht (BR-CO-16)")
    return out


def embed(pdf_a3: bytes, xml: bytes) -> bytes:
    """`pdf_a3` must already be PDF/A-3b (the PDF service converts it);
    facturx attaches the XML and writes the XMP metadata."""
    from facturx import generate_from_binary
    return generate_from_binary(pdf_a3, xml, flavor="factur-x", level=LEVEL, check_xsd=True, check_schematron=False, afrelationship="data")


def make(pdf_a3: bytes, payload: Dict[str, Any]) -> Dict[str, Any]:
    """→ {ok, pdf, problems}. Never raises: the caller shows the problems."""
    try:
        xml = build_xml(payload)
        problems = check_xml(xml)
        if problems:
            return {"ok": False, "pdf": None, "problems": problems}
        hybrid = embed(pdf_a3, xml)
        from facturx import get_xml_from_pdf
        name, back = get_xml_from_pdf(hybrid, check_xsd=False)
        if not back:
            return {"ok": False, "pdf": None, "problems": ["Das XML ließ sich nicht aus dem fertigen PDF zurücklesen."]}
        return {"ok": True, "pdf": hybrid, "problems": []}
    except Exception as exc:  # noqa: BLE001
        log.warning("e-invoice failed: %s", exc)
        return {"ok": False, "pdf": None, "problems": [f"{type(exc).__name__}: {str(exc)[:400]}"]}
