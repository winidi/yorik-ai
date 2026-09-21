"""Schreiben, stage 1: a letterhead per person, fixed layouts, the
arithmetic of an invoice, the rows behind the documents."""

from __future__ import annotations

import io
from decimal import Decimal

import pytest

from tests.conftest import login_client


def _png() -> bytes:
    from PIL import Image
    b = io.BytesIO(); Image.new("RGBA", (300, 100), (180, 60, 90, 255)).save(b, "PNG")
    return b.getvalue()


def test_letterhead_starts_from_the_profile_and_is_yours_alone(fresh_app):
    from backend.database import get_conn
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET address_street='Beispielweg 1', address_postcode='01067', address_city='Dresden', "
                     "business_name='Beispiel Werkstatt', iban='DE89 3704 0044 0532 0130 00' WHERE id = ?", (dirk,)); conn.commit()
    got = dirk_c.get("/api/letterheads").json()
    assert len(got["letterheads"]) == 1 and len(got["fonts"]) >= 4
    mine = got["letterheads"][0]
    assert mine["is_default"] and mine["data"]["postcode"] == "01067" and mine["data"]["business_name"] == "Beispiel Werkstatt"
    assert dirk_c.get("/api/letterheads").json()["letterheads"][0]["id"] == mine["id"]          # asked twice, made once

    r = dirk_c.patch(f"/api/letterheads/{mine['id']}", json={"data": {"accent": "#b43c5a", "font": "carlito", "payment_days": 999, "nonsense": "x"}})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["accent"] == "#b43c5a" and d["font"] == "carlito" and d["payment_days"] == 365 and "nonsense" not in d
    assert d["postcode"] == "01067"                                                           # what was not sent stays
    assert dirk_c.patch(f"/api/letterheads/{mine['id']}", json={"data": {"accent": "red;}", "font": "comic"}}).json()["data"]["accent"] == "#1f3a5f"

    # Beate has her own and reaches neither Dirk's data nor his logo
    hers = beate_c.get("/api/letterheads").json()["letterheads"][0]
    assert hers["id"] != mine["id"] and hers["data"]["business_name"] == ""
    assert beate_c.patch(f"/api/letterheads/{mine['id']}", json={"data": {"iban": "XX"}}).status_code == 404
    assert dirk_c.post(f"/api/letterheads/{mine['id']}/logo", files={"image": ("logo.png", _png(), "image/png")}).json()["logo_url"]
    assert dirk_c.get(f"/api/letterheads/{mine['id']}/logo").status_code == 200
    assert beate_c.get(f"/api/letterheads/{mine['id']}/logo").status_code == 404
    assert dirk_c.post(f"/api/letterheads/{mine['id']}/logo", files={"image": ("x.png", b"not an image", "image/png")}).status_code == 400
    assert dirk_c.delete(f"/api/letterheads/{mine['id']}/logo").json()["logo_url"] is None


def test_two_people_two_looks_and_unsaved_changes_in_the_preview(fresh_app):
    dirk_c, _ = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, _ = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    lid = dirk_c.get("/api/letterheads").json()["letterheads"][0]["id"]
    dirk_c.patch(f"/api/letterheads/{lid}", json={"data": {"accent": "#0a7d4f", "font": "caladea"}})
    a = dirk_c.post("/api/letterheads/preview", json={"kind": "invoice"}).json()["html"]
    b = beate_c.post("/api/letterheads/preview", json={"kind": "invoice"}).json()["html"]
    assert "#0a7d4f" in a and "Caladea" in a and "#0a7d4f" not in b and "Liberation Sans" in b
    assert 'class="sheet"' in a and "571,08" in a and "Rechnung 2026-014" in a                # the same sample, his look
    # the form's unsaved state is laid over the stored letterhead
    live = dirk_c.post("/api/letterheads/preview", json={"kind": "letter", "data": {"business_name": "Neu & <b>frech</b>"}}).json()["html"]
    assert "Neu &amp; &lt;b&gt;frech&lt;/b&gt;" in live and "#0a7d4f" in live
    assert dirk_c.post("/api/letterheads/preview", json={"kind": "poster"}).status_code == 400


def test_layouts_mark_what_is_missing_and_never_take_a_look_from_the_content():
    from backend.writing import layouts as L, letterhead as H
    lh = H.clean({"sender_name": "Dirk", "street": "Weg 1", "postcode": "01067", "city": "Dresden"})
    page = L.render("letter", lh, {"name": "Hausverwaltung Müller"}, {"subject": "Heizung",
                    "text_html": '<p style="color:red" onclick="x()">Guten Tag<script>alert(1)</script></p><img src="http://evil/x.png">'})
    assert "Adresse fehlt" in page["html"] and "Empfänger fehlt" not in page["html"]
    assert "<script" not in page["html"].split("</style>")[1] and "onclick" not in page["html"] and "evil" not in page["html"] and "color:red" not in page["html"]
    assert "Seite" in page["footer_html"] and "01067 Dresden" in page["footer_html"]
    plain = L.render("letter", lh, None, {"text": "Hallo <Welt>\n\nzweiter Absatz"})
    assert "<p>Hallo &lt;Welt&gt;</p><p>zweiter Absatz</p>" in plain["html"] and "Empfänger fehlt" in plain["html"]
    assert "Noch keine Positionen" in L.render("quote", lh, None, {})["html"]
    with pytest.raises(ValueError):
        L.render("poster", lh, None, {})


def test_invoice_arithmetic_is_decimal_and_adds_up():
    from backend.writing import invoice as I
    calc = I.compute([{"text": "Wartung", "qty": 1, "unit_price": "180"}, {"text": "Pumpe", "qty": 1, "unit_price": "129,90"},
                      {"text": "Arbeit", "qty": "2,5", "unit_price": 68}, {"text": "Buch", "qty": 3, "unit_price": "9,99", "vat_percent": 7},
                      {"text": "", "unit_price": 0}, "rubbish"])
    t = calc["totals"]
    assert len(calc["lines"]) == 4 and t["net"] == Decimal("509.87")
    assert [(v["rate"], v["vat"]) for v in t["vat_rows"]] == [(Decimal("7"), Decimal("2.10")), (Decimal("19"), Decimal("91.18"))]
    assert t["gross"] == t["net"] + t["vat"] == Decimal("603.15")
    assert I.compute([{"text": "x", "qty": 3, "unit_price": "0.335"}])["lines"][0]["net"] == Decimal("1.01")       # half-up, not banker's
    small = I.compute([{"text": "Beratung", "qty": 2, "unit_price": 50}], small_business=True)["totals"]
    assert small["vat"] == 0 and small["gross"] == Decimal("100.00") and small["vat_rows"] == []
    assert I.money(Decimal("1234.5")) == "1.234,50 €" and I.money(Decimal("1234.5"), "US", "$") == "1,234.50 $"
    assert I.dec("1.234,56") == Decimal("1234.56") and I.dec("nonsense") == 0


def test_documents_are_drafts_until_final_and_children_write_letters_only(fresh_app):
    from backend.database import get_conn
    from backend.writing import store as S
    _, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    _, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    doc = S.create(dirk, "invoice", title="Wartung", recipient={"name": "Mustermann GmbH"}, content={"lines": [{"text": "x", "unit_price": 1}]})
    assert doc["status"] == "draft" and doc["number"] is None and doc["content"]["lines"][0]["text"] == "x"
    assert S.get(doc["id"], beate) is None and S.list_for(beate) == [] and S.update(doc["id"], beate, title="meins") is None
    assert S.update(doc["id"], dirk, title="Wartung 2026")["title"] == "Wartung 2026"
    assert [d["id"] for d in S.list_for(dirk, status="draft")] == [doc["id"]] and "content" not in S.list_for(dirk)[0]
    with get_conn() as conn:
        conn.execute("UPDATE written_documents SET status = 'final', number = '2026-001' WHERE id = ?", (doc["id"],)); conn.commit()
    with pytest.raises(S.Locked):
        S.update(doc["id"], dirk, title="nachträglich")
    with pytest.raises(S.Locked):
        S.delete(doc["id"], dirk)
    draft = S.create(dirk, "letter")
    assert S.delete(draft["id"], dirk) is True and S.get(draft["id"], dirk) is None
    assert S.kinds_for("restricted") == ("letter",) and "invoice" in S.kinds_for("member")
    with pytest.raises(ValueError):
        S.create(dirk, "poster")


def test_sample_pdf_asks_the_pdf_service_with_footer_and_pdfa(fresh_app, monkeypatch):
    from backend.compose import pdf as P
    seen = {}

    class _R:
        ok = True; status_code = 200; content = b"%PDF-1.7 fake"; text = ""

    def fake_post(url, files=None, data=None, timeout=None):
        seen.update(url=url, files=files, data=data)
        return _R()

    monkeypatch.setattr(P.requests, "post", fake_post)
    client, _ = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    lid = client.get("/api/letterheads").json()["letterheads"][0]["id"]
    r = client.get(f"/api/letterheads/{lid}/sample.pdf?kind=invoice")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert set(seen["files"]) == {"index.html", "footer.html"} and seen["data"]["preferCssPageSize"] == "true" and "pdfa" not in seen["data"]
    assert P.render_html_pdf("<html></html>", pdfa="PDF/A-3b") and seen["data"]["pdfa"] == "PDF/A-3b"
    assert client.get("/api/letterheads/99999/sample.pdf").status_code == 404
