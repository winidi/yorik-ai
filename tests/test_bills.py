"""Bills (Dirk 2026-10-02): the Finance → Bills tab, the bank ticking a
bill off, due reminders, a photographed letter becoming a proposal, and
the bell's "Add to bills" working again."""

from __future__ import annotations

import io
from datetime import date, timedelta

import pytest

from tests.conftest import login_client, seed_user


def _ctx(uid, role="admin"):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=uid)


@pytest.fixture
def house(fresh_app):
    """Dirk (admin, Finance space), Beate (member, not in Finance), a
    child; Dirk's bank account with a few bookings."""
    from backend import spaces
    from backend.database import get_conn
    dirk = seed_user(name="Dirk", role="admin", email="dirk@example.com")
    beate = seed_user(name="Beate", role="member", email="beate@example.com")
    kid = seed_user(name="Yarik", role="restricted", email="yarik@example.com")
    spaces.ensure_workspace_exists(dirk, "Dirk")
    for u, n in ((dirk, "Dirk"), (beate, "Beate"), (kid, "Yarik")):
        spaces.ensure_personal_space(u, n)
    today = date.today()
    d = lambda n: (today + timedelta(days=n)).isoformat()
    with get_conn() as conn:
        acc = conn.execute("INSERT INTO bank_accounts (owner_user_id, space_id, display_name, bank_url, blz, login_name, "
                           "credential_key) VALUES (?, NULL, 'Giro', 'https://example.invalid', '0', 'x', 'u') RETURNING id",
                           (dirk,)).fetchone()["id"]
        for i, (days, amt, cp, purpose) in enumerate((
                (-2, -120.50, "STADTWERKE PEINE", "Rechnung 2026-4711 Strom"),
                (-1, -29.99, "VODAFONE GMBH", "Kundennummer 123"),
                (-1, -29.99, "AMAZON", "Bestellung"),
                (0, 50.00, "ARBEITGEBER", "Gehalt"))):
            conn.execute("INSERT INTO bank_transactions (account_id, booking_date, amount, counterparty, purpose, category, "
                         "dedup_hash) VALUES (?, ?, ?, ?, ?, 'x', ?)", (acc, d(days), amt, cp, purpose, f"h{i}"))
        conn.commit()
    return {"app": fresh_app, "dirk": dirk, "beate": beate, "kid": kid, "today": today, "d": d}


# ─── create, list, visibility ───────────────────────────────────────

def test_create_and_list_follow_spaces(house):
    from backend import bills
    b = bills.create_bill(user_id=house["dirk"], name="Strom März", amount=120.5, payee="Stadtwerke Peine",
                          due_date=house["d"](10), source="manual")
    assert b["paid"] is False and b["days_left"] == 10 and b["overdue"] is False
    assert [x["id"] for x in bills.list_bills(house["dirk"], "admin")] == [b["id"]]
    # Beate is not in Finance and did not record it: not hers to see.
    assert bills.list_bills(house["beate"], "member") == []
    # Her own bill she sees, Dirk (admin, Finance) sees it too.
    hers = bills.create_bill(user_id=house["beate"], name="Yoga", amount=40, due_date=house["d"](3))
    assert [x["id"] for x in bills.list_bills(house["beate"], "member")] == [hers["id"]]
    assert {x["id"] for x in bills.list_bills(house["dirk"], "admin")} == {b["id"], hers["id"]}


def test_summary_counts_overdue_and_due_soon(house):
    from backend import bills
    bills.create_bill(user_id=house["dirk"], name="alt", amount=10, due_date=house["d"](-2))
    bills.create_bill(user_id=house["dirk"], name="bald", amount=20, due_date=house["d"](2))
    bills.create_bill(user_id=house["dirk"], name="später", amount=30, due_date=house["d"](20))
    s = bills.summary(house["dirk"], "admin")
    assert s["open"] == 3 and s["overdue"] == 1 and s["due_soon"] == 1
    assert s["totals"] == {"EUR": 60.0} and s["next"]["name"] == "alt"


def test_rest_routes(house):
    client, uid = login_client(house["app"], role="admin", name="Dirk2", email="dirk2@example.com")
    r = client.post("/api/bills", json={"name": "GEZ", "amount": 18.36, "due_date": house["d"](5)})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    assert [b["id"] for b in client.get("/api/bills").json()] == [bid]
    r = client.patch(f"/api/bills/{bid}", json={"amount": 18.5, "payee": "ARD ZDF"})
    assert r.status_code == 200 and r.json()["amount"] == 18.5 and r.json()["payee"] == "ARD ZDF"
    r = client.post(f"/api/bills/{bid}/paid")
    assert r.status_code == 200 and r.json()["paid"] is True and r.json()["paid_by"] == "hand"
    assert client.get("/api/bills").json() == []
    assert [b["id"] for b in client.get("/api/bills?status=paid").json()] == [bid]
    assert client.post(f"/api/bills/{bid}/unpaid").json()["paid"] is False
    assert client.get("/api/bills/summary").json()["open"] == 1
    assert client.delete(f"/api/bills/{bid}").status_code == 200
    assert client.get("/api/bills").json() == []


def test_child_has_no_bills_and_members_cannot_touch_others(house):
    kid, _ = login_client(house["app"], role="restricted", name="Kid", email="kid2@example.com")
    assert kid.get("/api/bills").status_code == 403
    from backend import bills
    b = bills.create_bill(user_id=house["dirk"], name="Strom", amount=1, due_date=house["d"](1))
    member, _ = login_client(house["app"], role="member", name="Gast", email="gast@example.com")
    assert member.get("/api/bills").json() == []
    assert member.post(f"/api/bills/{b['id']}/paid").status_code == 403
    assert member.delete(f"/api/bills/{b['id']}").status_code == 403


# ─── the bank pays ──────────────────────────────────────────────────

def test_settle_with_bank_by_number_amount_and_payee(house):
    from backend import bills
    strom = bills.create_bill(user_id=house["dirk"], name="Strom", amount=118.00, payee="Stadtwerke",
                              number="2026-4711", due_date=house["d"](5))          # number wins, amount differs
    voda = bills.create_bill(user_id=house["dirk"], name="Handy", amount=29.99, payee="Vodafone",
                             due_date=house["d"](3))                                # amount + payee word
    other = bills.create_bill(user_id=house["dirk"], name="Fitness", amount=29.99, payee="McFit",
                              due_date=house["d"](3))                               # same amount, no payee word
    assert bills.settle_with_bank() == 2
    assert bills.get_bill(strom["id"])["paid"] is True
    assert bills.get_bill(strom["id"])["paid_by"] == "bank"
    assert bills.get_bill(voda["id"])["paid"] is True
    assert bills.get_bill(other["id"])["paid"] is False
    # Each booking pays one bill only: a second Vodafone bill stays open.
    voda2 = bills.create_bill(user_id=house["dirk"], name="Handy 2", amount=29.99, payee="Vodafone",
                              due_date=house["d"](3))
    assert bills.settle_with_bank() == 0
    assert bills.get_bill(voda2["id"])["paid"] is False


def test_bank_match_respects_date_window(house):
    from backend import bills
    late = bills.create_bill(user_id=house["dirk"], name="Strom", amount=120.50, payee="Stadtwerke",
                             due_date=house["d"](80))                                # booking 82 days before due
    assert bills.settle_with_bank() == 0
    assert bills.get_bill(late["id"])["paid"] is False


# ─── reminders ──────────────────────────────────────────────────────

def test_remind_due_once_per_stage(house):
    from backend import bills, notifications
    bills.create_bill(user_id=house["dirk"], name="Miete", amount=900, due_date=house["d"](2))
    bills.create_bill(user_id=house["dirk"], name="Später", amount=5, due_date=house["d"](10))
    overdue = bills.create_bill(user_id=house["dirk"], name="Alt", amount=7, due_date=house["d"](-1))
    assert bills.remind_due() == 2
    assert bills.remind_due() == 0                       # not twice
    rows = notifications.list_for_user(house["dirk"])
    kinds = sorted((n["kind"], n["title"]) for n in rows)
    assert kinds == [("bill_due", "Bill due in 2 days: Miete"), ("bill_due", "Bill overdue: Alt")]
    assert all(n["navigate_to"] == "/r/finance?tab=bills" for n in rows)
    # A soon-reminded bill that becomes overdue is reminded once more.
    bills.update_bill(overdue["id"], due_date=house["d"](1))
    with __import__("backend.database", fromlist=["get_conn"]).get_conn() as conn:
        conn.execute("UPDATE bills SET reminded_at = 'soon' WHERE id = ?", (overdue["id"],))
        conn.commit()
    assert bills.remind_due() == 0
    bills.update_bill(overdue["id"], due_date=house["d"](-1))
    assert bills.remind_due() == 1


# ─── a letter becomes a proposal ────────────────────────────────────

LETTER = ("Stadtwerke Peine GmbH\nRechnung Nr. 2026-4711\nRechnungsdatum 01.10.2026\n"
          "Stromlieferung März\nGesamtbetrag 120,50 EUR\nZahlbar bis 15.10.2026")


def test_photographed_letter_is_proposed_once(house, monkeypatch):
    from backend import bills, notifications
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET paperless_user_id = 7 WHERE id = ?", (house["dirk"],))
        conn.commit()
    calls = []

    def fake_read(text, title=""):
        calls.append(title)
        return {"is_bill": True, "payee": "Stadtwerke Peine", "amount": 120.5, "currency": "EUR",
                "bill_date": "2026-10-01", "due_date": "2026-10-15", "paid": False, "number": "2026-4711"}
    monkeypatch.setattr(bills, "read_document", fake_read)
    doc = {"id": 55, "title": "Strom März", "content": LETTER, "owner": 7, "added": house["today"].isoformat(),
           "correspondent": None}
    nid = bills.consider_paperless_document(55, doc)
    assert nid
    n = notifications.list_for_user(house["dirk"])[0]
    assert n["kind"] == "document_proposal" and n["title"] == "New bill from Stadtwerke Peine?"
    assert n["payload"]["extracted"] == {"amount": 120.5, "currency": "EUR", "due_date": "2026-10-15"}
    assert n["navigate_to"] == "/r/documents?doc=55&source=paperless"
    # Re-indexed later: read once, proposed once.
    assert bills.consider_paperless_document(55, doc) is None
    assert calls == ["Strom März"]
    # An old document that is merely re-indexed is not read at all.
    old = {**doc, "id": 56, "added": (house["today"] - timedelta(days=30)).isoformat()}
    assert bills.consider_paperless_document(56, old) is None
    assert calls == ["Strom März"]
    # A receipt (paid) or no owner: nothing.
    monkeypatch.setattr(bills, "read_document", lambda t, title="": {**fake_read(t, title), "paid": True})
    assert bills.consider_paperless_document(57, {**doc, "id": 57}) is None
    assert bills.consider_paperless_document(58, {**doc, "id": 58, "owner": 99}) is None


def test_accepting_a_document_proposal_records_the_bill(house, monkeypatch):
    from backend import bills
    from backend.database import get_conn
    client, uid = login_client(house["app"], role="admin", name="Dirk3", email="dirk3@example.com")
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET paperless_user_id = 8 WHERE id = ?", (uid,))
        conn.commit()
    monkeypatch.setattr(bills, "read_document", lambda t, title="": {
        "is_bill": True, "payee": "Stadtwerke Peine", "amount": 120.5, "currency": "EUR",
        "bill_date": "2026-10-01", "due_date": "2026-10-15", "paid": False, "number": "2026-4711"})
    nid = bills.consider_paperless_document(61, {"id": 61, "title": "Strom", "content": LETTER, "owner": 8,
                                                 "added": house["today"].isoformat()})
    r = client.post(f"/api/notifications/{nid}/accept")
    assert r.status_code == 200, r.text
    rows = client.get("/api/bills").json()
    assert len(rows) == 1
    b = rows[0]
    assert (b["name"], b["payee"], b["amount"], b["due_date"], b["number"], b["source"], b["source_ref"]) == \
           ("Strom", "Stadtwerke Peine", 120.5, "2026-10-15", "2026-4711", "paperless", 61)
    assert b["link"] == "/r/documents?doc=61&source=paperless"
    # The bank booking with that invoice number ticks it off.
    with get_conn() as conn:
        acc = conn.execute("INSERT INTO bank_accounts (owner_user_id, space_id, display_name, bank_url, blz, login_name, "
                           "credential_key) VALUES (?, NULL, 'Giro', 'https://example.invalid', '0', 'x', 'u') RETURNING id",
                           (uid,)).fetchone()["id"]
        conn.execute("INSERT INTO bank_transactions (account_id, booking_date, amount, counterparty, purpose, category, "
                     "dedup_hash) VALUES (?, ?, -121.00, 'STADTWERKE', 'RG 2026-4711 inkl. Mahngebuehr', 'x', 'z1')",
                     (acc, house["today"].isoformat()))
        conn.commit()
    paid = client.get("/api/bills?status=paid").json()
    assert client.get("/api/bills").json() == [] and paid[0]["paid_by"] == "bank"


def test_read_document_drops_amounts_not_in_text(monkeypatch):
    from backend import bills

    class FakeClient:
        def __init__(self, **kw): pass
        def chat(self, messages, **kw):
            assert "<document>" in messages[1]["content"]
            return {"content": '{"is_bill": true, "payee": "X", "amount": 999.99, "currency": "EUR", '
                               '"bill_date": "2026-10-01", "due_date": null, "paid": false, "number": null}'}
    import backend.agent.llm as llm
    monkeypatch.setattr(llm, "LlmClient", FakeClient)
    out = bills.read_document("Gesamtbetrag 120,50 EUR", "Strom")
    assert out["is_bill"] is True and out["amount"] is None


# ─── the chat skills ────────────────────────────────────────────────

def test_chat_skills_see_only_the_persons_bills(house):
    import asyncio
    from backend import bills
    from backend.skills.registry import get_registry
    reg = get_registry()
    assert {"add_bill", "check_bills", "delete_bill", "find_bill_by_name", "update_bill"} <= set(reg._skills)
    assert "restricted" not in reg._skills["check_bills"].permissions

    async def run():
        out = await reg.invoke("add_bill", ctx=_ctx(house["dirk"]), name="Strom", amount=120.5,
                               payee="Stadtwerke", due_date=house["d"](5))
        assert out["bill"]["payee"] == "Stadtwerke" and out["bill"]["source"] == "chat"
        mine = await reg.invoke("check_bills", ctx=_ctx(house["dirk"]))
        assert mine["count"] == 1 and "shown_to_user" in mine["_llm_hint"]
        theirs = await reg.invoke("check_bills", ctx=_ctx(house["beate"], "member"))
        assert theirs["count"] == 0
        found = await reg.invoke("find_bill_by_name", ctx=_ctx(house["dirk"]), query="stadtwerke")
        assert found["count"] == 1
        upd = await reg.invoke("update_bill", ctx=_ctx(house["dirk"]), bill_id=out["bill_id"], paid=True)
        assert upd["bill"]["paid"] is True and upd["bill"]["paid_by"] == "hand"
        with pytest.raises(Exception):
            await reg.invoke("delete_bill", ctx=_ctx(house["beate"], "member"), bill_id=out["bill_id"])
        staged = await reg.invoke("delete_bill", ctx=_ctx(house["dirk"]), bill_id=out["bill_id"])
        assert staged["pending"] is True
        assert bills.get_bill(out["bill_id"]) is not None          # nothing gone before the tap
        from backend import pending_actions as pa
        pa.apply(staged["pending_id"])                             # "Delete" on the card
        assert bills.get_bill(out["bill_id"]) is None
    asyncio.run(run())


# ─── a photo becomes a PDF ──────────────────────────────────────────

def test_photo_to_pdf_is_upright_pdf():
    from PIL import Image
    from backend import documents
    buf = io.BytesIO()
    Image.new("RGB", (3000, 4000), "white").save(buf, format="JPEG")
    pdf = documents.photo_to_pdf(buf.getvalue())
    assert pdf[:4] == b"%PDF"
    assert documents.is_photo("image/jpeg") and documents.is_photo(None, "brief.PNG")
    assert not documents.is_photo("application/pdf", "x.pdf")
    with pytest.raises(ValueError):
        documents.photo_to_pdf(b"not an image")


def test_upload_accepts_a_photo(house, monkeypatch):
    from PIL import Image
    import backend.main as main
    monkeypatch.setattr(main, "_push_to_paperless", lambda *a, **k: {"ok": False, "skipped": True})
    client, uid = login_client(house["app"], role="admin", name="Dirk4", email="dirk4@example.com")
    buf = io.BytesIO()
    Image.new("RGB", (800, 1000), "white").save(buf, format="JPEG")
    r = client.post("/api/documents/upload", files={"file": ("brief.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 201, r.text
    meta = r.json()
    assert meta["title"] == "brief" and meta.get("mime_type", "application/pdf") == "application/pdf"
