"""A letter from a person: the full name, said twice at most, and the
choice to send the letter as the mail itself.

From a cancellation to a gym (2026-09-24): the letter said the first
name four times (letterhead, sender line, signature, footer) — the
letterhead had been made from the display name, not from first and
last name — and it could only leave as a PDF attached to a mail."""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import login_client
from tests.test_writing_letters import pdf_service  # noqa: F401  (fixture)


def _named_user(fresh_app, first="Beate", last="Muster"):
    from backend.database import get_conn
    client, uid = login_client(fresh_app, role="member", name=first, email=f"{first.lower()}@example.local")
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET first_name=?, last_name=?, address_street='Hauptstr. 1', "
                     "address_postcode='12345', address_city='Musterstadt' WHERE id=?", (first, last, uid))
        conn.commit()
    return client, uid


def _body(html: str) -> str:
    return re.sub(r"<style>.*?</style>", "", html, flags=re.S)


def test_a_new_letterhead_carries_first_and_last_name(fresh_app):
    client, _ = _named_user(fresh_app)
    lh = client.get("/api/letterheads").json()["letterheads"][0]["data"]
    assert lh["sender_name"] == "Beate Muster" and lh["signature_name"] == "Beate Muster"


def test_the_correction_takes_the_display_name_out_of_old_letterheads(fresh_app):
    """Migration 158 on the letterheads made before this fix: the display
    name becomes the full name, a name somebody typed in stays."""
    import json
    from backend.database import get_conn
    _, beate = _named_user(fresh_app, "Beate", "Muster")
    _, dirk = _named_user(fresh_app, "Dirk", "Beispiel")
    with get_conn() as conn:
        conn.execute("DELETE FROM letterheads")
        conn.execute("INSERT INTO letterheads (user_id, data) VALUES (?, ?)",
                     (beate, json.dumps({"sender_name": "Beate", "signature_name": "Beate"})))
        conn.execute("INSERT INTO letterheads (user_id, data) VALUES (?, ?)",
                     (dirk, json.dumps({"sender_name": "Dirk", "signature_name": "D. Beispiel, Tischlermeister"})))
        conn.execute((Path(__file__).resolve().parent.parent / "migrations_pg" / "158_letterhead_full_name.sql").read_text())
        conn.commit()
        rows = {r["user_id"]: json.loads(r["data"]) for r in conn.execute("SELECT user_id::text AS user_id, data FROM letterheads").fetchall()}
    assert rows[beate] == {"sender_name": "Beate Muster", "signature_name": "Beate Muster"}
    assert rows[dirk] == {"sender_name": "Dirk Beispiel", "signature_name": "D. Beispiel, Tischlermeister"}


def test_a_private_letter_names_the_sender_twice():
    from backend.writing import layouts as L
    lh = {"sender_name": "Beate Muster", "signature_name": "Beate Muster", "street": "Hauptstr. 1",
          "postcode": "12345", "city": "Musterstadt", "phone": "0123 456"}
    content = {"subject": "Kündigung", "text_html": "<p>Sehr geehrte Damen und Herren,</p><p>hiermit kündige ich.</p>",
               "add_closing": True}
    to = {"name": "Kampfsportschule", "address_lines": ["Weg 2", "12345 Musterstadt"]}
    private = L.render("letter", lh, to, content)
    assert _body(private["html"]).count("Beate Muster") == 2           # sender line, signature
    assert 'class="who"' not in _body(private["html"]) and "Beate" not in private["footer_html"]
    assert "0123 456" in private["footer_html"]                        # contact stays in the footer

    business = L.render("letter", dict(lh, business_name="Muster Design"), to, content)
    assert 'class="who"' in _body(business["html"]) and "Muster Design" in business["footer_html"]

    chosen = L.render("letter", dict(lh, style="business"), to, content)
    assert 'class="who"' in _body(chosen["html"])                      # a person may choose the letterhead
    chosen = L.render("letter", dict(lh, business_name="Muster Design", style="private"), to, content)
    assert 'class="who"' not in _body(chosen["html"])

    invoice = L.render("invoice", lh, to, {"lines": [{"text": "Beratung", "qty": 1, "unit_price": "80"}]})
    assert 'class="who"' in _body(invoice["html"])                     # an invoice keeps its head


def test_a_letter_can_leave_as_the_mail_itself(fresh_app, pdf_service, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("YORIK_WRITTEN_DIR", str(tmp_path / "written"))
    from backend import email_sender
    from backend.database import get_conn
    client, uid = _named_user(fresh_app)
    with get_conn() as conn:
        acc = conn.execute(
            "INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_port, imap_username, smtp_host, "
            "smtp_port, smtp_username, credential_key) VALUES (?, 'beate@example.local', 'i', 993, 'b', 's', 465, 'b', 'k') "
            "RETURNING id", (uid,)).fetchone()["id"]
        conn.commit()
    did = client.post("/api/writing", json={"kind": "letter", "content": {
        "subject": "Kündigung der Mitgliedschaft", "add_closing": True,
        "text": "Sehr geehrte Damen und Herren,\n\nhiermit kündige ich meine Mitgliedschaft zum nächstmöglichen Termin."}}).json()["id"]
    sent = {}
    monkeypatch.setattr(email_sender, "send", lambda account_id, to, subject, body_text, **kw: (
        sent.update(subject=subject, text=body_text, html=kw.get("body_html"), att=kw.get("attachments")), {"ok": True})[1])

    r = client.post(f"/api/writing/{did}/send", json={"account_id": acc, "to": "info@schule.example", "subject": "",
                                                       "send_as": "text"})
    assert r.status_code == 200, r.text
    assert sent["att"] is None and sent["subject"] == "Kündigung der Mitgliedschaft"
    assert sent["text"].startswith("Sehr geehrte Damen und Herren,") and sent["text"].count("Beate Muster") == 1
    assert sent["text"].rstrip().endswith("Mit freundlichen Grüßen\nBeate Muster")
    assert "Hauptstr" not in sent["text"] and "Beate Muster" in sent["html"]
    assert r.json()["document"]["status"] == "final"                   # the PDF is kept all the same


def test_an_invoice_only_leaves_as_a_pdf(fresh_app, pdf_service, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("YORIK_WRITTEN_DIR", str(tmp_path / "written"))
    from backend.database import get_conn
    client, uid = _named_user(fresh_app)
    with get_conn() as conn:
        acc = conn.execute(
            "INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_port, imap_username, smtp_host, "
            "smtp_port, smtp_username, credential_key) VALUES (?, 'beate@example.local', 'i', 993, 'b', 's', 465, 'b', 'k') "
            "RETURNING id", (uid,)).fetchone()["id"]
        conn.execute("UPDATE written_documents SET status='final'")
        conn.commit()
    did = client.post("/api/writing", json={"kind": "invoice", "content": {"lines": []}}).json()["id"]
    with get_conn() as conn:
        conn.execute("UPDATE written_documents SET status='final' WHERE id=?", (did,))
        conn.commit()
    r = client.post(f"/api/writing/{did}/send", json={"account_id": acc, "to": "k@example.local", "subject": "x", "send_as": "text"})
    assert r.status_code == 400
