"""Schreiben, stage 3: invoices and quotes. Data in, a fixed look out;
the number is taken only when everything else has worked; the e-invoice
is built from the same figures and checked before anybody gets it."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import login_client

LINES = [{"text": "Wartung Heizungsanlage", "qty": "1", "unit": "pauschal", "unit_price": "180"},
         {"text": "Arbeitszeit", "qty": "2,5", "unit": "Std.", "unit_price": "68"}]
CUSTOMER = {"name": "Mustermann GmbH", "address_lines": ["Hauptstraße 99", "10115 Berlin"]}
SELLER = {"sender_name": "Dirk Winiecki", "business_name": "Winiecki Media", "street": "Beispielweg 1", "postcode": "01067", "city": "Dresden",
          "vat_id": "DE123456789", "iban": "DE89 3704 0044 0532 0130 00"}


@pytest.fixture
def shop(fresh_app, monkeypatch, tmp_path):
    """A signed-in business owner with a letterhead, and a PDF service that records what it was asked."""
    monkeypatch.setenv("YORIK_WRITTEN_DIR", str(tmp_path / "written"))
    from backend.compose import pdf as P
    asked = []

    class _R:
        ok = True; status_code = 200; content = b"%PDF-1.7 invoice"; text = ""

    monkeypatch.setattr(P.requests, "post", lambda url, files=None, data=None, timeout=None: (asked.append({"html": files["index.html"][1], "data": data}), _R())[1])
    client, uid = login_client(fresh_app, role="member", name="Dirk", email="d@example.local")
    lid = client.get("/api/letterheads").json()["letterheads"][0]["id"]
    client.patch(f"/api/letterheads/{lid}", json={"data": SELLER})
    return client, uid, asked, lid


def test_a_draft_says_what_is_missing_and_a_failure_costs_no_number(shop):
    client, uid, asked, lid = shop
    did = client.post("/api/writing", json={"kind": "invoice", "recipient": {"name": "Mustermann GmbH"}, "content": {"subject": "Wartung", "lines": LINES}}).json()["id"]
    st = client.get(f"/api/writing/{did}/state").json()
    assert st["missing"] == ["Anschrift des Kunden (Straße, PLZ, Ort)", "Leistungsdatum oder -zeitraum"]
    assert st["totals"]["net"] == "350,00 €" and st["totals"]["gross"] == "416,50 €" and st["totals"]["lines"] == ["180,00 €", "170,00 €"]
    year = date.today().year
    assert st["next_number"] == f"{year}-001" and st["e_invoice"] == {"wanted": True, "available": st["e_invoice"]["available"], "result": None}
    r = client.post(f"/api/writing/{did}/finalise", json={})
    assert r.status_code == 422 and r.json()["detail"]["missing"] == st["missing"]
    for route, body in (("send", {"account_id": 1, "to": "a@b.example", "subject": "x"}), ("paperless", {})):     # a draft invoice does not leave the house
        assert client.post(f"/api/writing/{did}/{route}", json=body).status_code in (404, 409)
    assert client.get(f"/api/writing/{did}").json()["status"] == "draft" and client.get(f"/api/writing/{did}/state").json()["next_number"] == f"{year}-001"


def test_finalising_numbers_in_order_and_freezes_the_invoice(shop, monkeypatch):
    from backend.writing import einvoice
    monkeypatch.setattr(einvoice, "available", lambda: False)
    client, uid, asked, lid = shop
    year = date.today().year
    ids = [client.post("/api/writing", json={"kind": "invoice", "recipient": CUSTOMER, "content": {"subject": f"Auftrag {i}", "lines": LINES, "service_from": "2026-09-01", "service_to": "2026-09-12"}}).json()["id"] for i in (1, 2)]
    # Germany wants an e-invoice; without the extension the person is told and decides
    r = client.post(f"/api/writing/{ids[0]}/finalise", json={})
    assert r.status_code == 409 and r.json()["detail"]["can_continue_without"] is True
    assert client.get(f"/api/writing/{ids[0]}/state").json()["next_number"] == f"{year}-001"                       # nothing was taken
    done = client.post(f"/api/writing/{ids[0]}/finalise", json={"without_e_invoice": True})
    assert done.status_code == 200, done.text
    doc = done.json()["document"]
    assert doc["status"] == "final" and doc["number"] == f"{year}-001" and doc["has_pdf"] and doc["content"]["e_invoice"] == {"format": None, "checked": False}
    assert asked[-1]["data"].get("pdfa") == "PDF/A-3b" and f"Rechnung {year}-001" in asked[-1]["html"] and "416,50" in asked[-1]["html"]
    assert client.post(f"/api/writing/{ids[1]}/finalise", json={"without_e_invoice": True}).json()["document"]["number"] == f"{year}-002"
    assert client.post(f"/api/writing/{ids[0]}/finalise", json={}).json()["document"]["number"] == f"{year}-001"    # asked twice, numbered once
    assert client.patch(f"/api/writing/{ids[0]}", json={"content": {"lines": []}}).status_code == 409
    assert client.delete(f"/api/writing/{ids[0]}").status_code == 409
    pdf = client.get(f"/api/writing/{ids[0]}/pdf")
    assert pdf.status_code == 200 and f"Rechnung%20{year}-001.pdf" in pdf.headers["content-disposition"]
    from backend.compose import series as S
    ser = S.default_for_kind("rechnung", owner_user_id=uid)
    assert [a["formatted"] for a in S.list_allocations(ser["id"])][::-1] == [f"{year}-001", f"{year}-002"] or len(S.list_allocations(ser["id"])) == 2


def test_a_quote_gets_its_own_number_and_becomes_an_invoice(shop):
    client, uid, asked, lid = shop
    year = date.today().year
    q = client.post("/api/writing", json={"kind": "quote", "recipient": {"name": "Mustermann GmbH"}, "content": {"subject": "Neue Heizung", "lines": LINES}}).json()
    assert client.get(f"/api/writing/{q['id']}/state").json()["missing"] == []                                   # a quote needs little
    final = client.post(f"/api/writing/{q['id']}/finalise", json={}).json()["document"]
    assert final["number"] == f"A-{year}-001" and "pdfa" not in asked[-1]["data"] and f"Angebot A-{year}-001" in asked[-1]["html"]
    inv = client.post(f"/api/writing/{q['id']}/to-invoice").json()
    assert inv["kind"] == "invoice" and inv["status"] == "draft" and inv["source_document_id"] == q["id"] and inv["number"] is None
    assert [l["text"] for l in inv["content"]["lines"]] == ["Wartung Heizungsanlage", "Arbeitszeit"] and f"A-{year}-001" in inv["content"]["intro_html"]
    assert client.post(f"/api/writing/{inv['id']}/to-invoice").status_code == 400
    kid_c, _ = login_client(client.app, role="restricted", name="Yorik", email="k@example.local")
    assert kid_c.post("/api/writing", json={"kind": "quote"}).status_code == 403


def test_small_business_charges_no_vat_and_says_so(shop):
    client, uid, asked, lid = shop
    client.patch(f"/api/letterheads/{lid}", json={"data": {"small_business": True, "vat_id": "", "tax_id": "12/345/67890"}})
    did = client.post("/api/writing", json={"kind": "invoice", "recipient": CUSTOMER, "content": {"lines": LINES, "service_from": "2026-09-01"}}).json()["id"]
    st = client.get(f"/api/writing/{did}/state").json()
    assert st["missing"] == [] and st["totals"]["gross"] == "350,00 €" and st["totals"]["vat_rows"] == []
    assert "§ 19 UStG" in client.get(f"/api/writing/{did}/preview").json()["html"]


# ── the e-invoice itself (needs the extension's libraries) ──────────────

def _payload(letterhead, lines=LINES):
    from backend.writing import invoice as I, letterhead as H
    return I.einvoice_payload(H.clean(letterhead), CUSTOMER, {"lines": lines, "service_from": "2026-09-01", "service_to": "2026-09-12"},
                              number="2026-001", issue_date="2026-09-22", due_date="2026-10-06")


@pytest.mark.parametrize("name,letterhead,lines", [
    ("standard", SELLER, LINES),
    ("two rates", SELLER, LINES + [{"text": "Fachbuch", "qty": "3", "unit_price": "9,99", "vat_percent": "7"}]),
    ("small business", {**SELLER, "vat_id": "", "tax_id": "12/345/67890", "small_business": True}, LINES),
])
def test_the_xml_passes_schema_and_business_rules(name, letterhead, lines):
    pytest.importorskip("drafthorse"); pytest.importorskip("facturx")
    from backend.writing import einvoice
    payload = _payload(letterhead, lines)
    assert payload["buyer"] == {"name": "Mustermann GmbH", "street": "Hauptstraße 99", "postcode": "10115", "city": "Berlin", "country": "DE"}
    xml = einvoice.build_xml(payload)
    assert einvoice.check_xml(xml) == [], name
    assert b"2026-001" in xml and payload["totals"]["gross"].encode() in xml


def test_a_wrong_sum_does_not_pass_the_check():
    pytest.importorskip("drafthorse"); pytest.importorskip("facturx")
    from backend.writing import einvoice
    payload = _payload(SELLER)
    payload["totals"]["gross"] = "999.99"
    assert einvoice.check_xml(einvoice.build_xml(payload)) != []


def test_a_complete_e_invoice_passes_the_reference_validator(tmp_path):
    """The whole way, for real: layout → PDF/A-3b from the PDF service →
    XML embedded → Mustang. Runs where the extension's libraries, the PDF
    service, Java and the Mustang jar (YORIK_MUSTANG_JAR) are present."""
    import os, shutil, subprocess
    pytest.importorskip("drafthorse"); pytest.importorskip("facturx")
    jar = os.getenv("YORIK_MUSTANG_JAR")
    if not (jar and os.path.exists(jar) and shutil.which("java")):
        pytest.skip("Mustang not set up")
    from backend.compose import pdf as P
    from backend.writing import einvoice, invoice as I, layouts as L, letterhead as H
    for seller in (SELLER, {**SELLER, "vat_id": "", "tax_id": "12/345/67890", "small_business": True}):
        lh = H.clean(seller)
        content = {"number": "2026-001", "date": "2026-09-22", "due_date": "2026-10-06", "lines": LINES, "service_from": "2026-09-01"}
        page = L.render("invoice", lh, CUSTOMER, content)
        blob = P.render_html_pdf(page["html"], footer_html=page["footer_html"], pdfa="PDF/A-3b")
        if not blob:
            pytest.skip("PDF service not reachable")
        made = einvoice.make(blob, I.einvoice_payload(lh, CUSTOMER, content, number="2026-001", issue_date="2026-09-22", due_date="2026-10-06"))
        assert made["ok"], made["problems"]
        path = tmp_path / "invoice.pdf"; path.write_bytes(made["pdf"])
        run = subprocess.run(["java", "-Xmx1G", "-jar", jar, "--no-notices", "--action", "validate", "--source", str(path)], capture_output=True, text=True, timeout=180)
        assert '<summary status="invalid"/>' not in run.stdout and '<summary status="valid"/>' in run.stdout, run.stdout[-1500:]


def test_the_chat_hands_over_data_and_the_app_computes(shop):
    import asyncio
    from backend import contacts as C
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.write_invoice.skill import execute
    client, uid, asked, lid = shop
    cid = C.create(display_name="Mustermann GmbH", kind="business", created_by_user_id=uid)
    C.add_address(int(cid["id"] if isinstance(cid, dict) else cid), kind="billing", line1="Hauptstraße 99", postcode="10115", city="Berlin", country="DE")
    ctx = SkillContext(Registry(), role="member", user_id=uid, conversation_id="c1")
    out = asyncio.run(execute(ctx, kind="Rechnung", customer="Mustermann GmbH", subject="Wartung Heizung",
                              lines=[{"text": "Wartung", "qty": 1, "unit": "pauschal", "unit_price": 180}, {"text": "Arbeitszeit", "qty": 2.5, "unit": "Std.", "unit_price": 68}]))
    assert out["total"] == "416,50 €" and out["missing"] == ["Leistungsdatum oder -zeitraum"] and "do not ask" in out["_llm_hint"]
    doc = client.get(f"/api/writing/{out['document_id']}").json()
    assert doc["kind"] == "invoice" and doc["status"] == "draft" and doc["number"] is None and doc["recipient"]["address_lines"] == ["Hauptstraße 99", "10115 Berlin"]
    again = asyncio.run(execute(ctx, kind="invoice", customer="Mustermann GmbH", lines=[{"text": "Wartung", "qty": 1, "unit_price": 200}], service_from="2026-09-01", document_id=out["document_id"]))
    assert again["document_id"] == out["document_id"] and again["total"] == "238,00 €" and again["missing"] == []
    assert client.get(f"/api/writing/{out['document_id']}").json()["title"] == "Wartung Heizung"
    kid = SkillContext(Registry(), role="restricted", user_id=uid, conversation_id="c2")
    with pytest.raises(ValueError):
        asyncio.run(execute(kid, kind="invoice", customer="x", lines=[]))


def test_an_invoice_over_nothing_asks_for_no_payment_and_a_single_day_is_a_date():
    from backend.writing import layouts as L, letterhead as H
    lh = H.clean(SELLER)
    free = L.render("invoice", lh, CUSTOMER, {"lines": [{"text": "Test", "qty": "1", "unit_price": "0"}], "service_from": "2026-09-22"})["html"]
    assert "überweisen" not in free and "Fällig am" not in free and "Leistungsdatum" in free and "Leistungszeitraum" not in free
    paid = L.render("invoice", lh, CUSTOMER, {"lines": LINES, "service_from": "2026-09-01", "service_to": "2026-09-12"})["html"]
    assert "überweisen" in paid and "Fällig am" in paid and "Leistungszeitraum" in paid
