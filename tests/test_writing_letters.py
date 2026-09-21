"""Schreiben, stage 2: letters. The chat writes a draft and never asks,
the contacts bring the address, a letter becomes final when it leaves."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import login_client


@pytest.fixture
def pdf_service(monkeypatch):
    """Gotenberg stands in: every render returns a small PDF."""
    from backend.compose import pdf as P
    calls = []

    class _R:
        ok = True; status_code = 200; content = b"%PDF-1.7 letter"; text = ""

    monkeypatch.setattr(P.requests, "post", lambda url, files=None, data=None, timeout=None: (calls.append((files, data)), _R())[1])
    return calls


def _ctx(uid, role="member"):
    from backend.skills.registry import Registry, SkillContext
    return SkillContext(Registry(), role=role, user_id=uid, conversation_id="c1")


def test_the_chat_writes_a_draft_and_marks_what_is_missing(fresh_app, tmp_path, monkeypatch):
    from backend import contacts as C, ui_tools
    from backend.skills.write_letter.skill import execute
    client, uid = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    cid = C.create(display_name="Hausverwaltung Müller", kind="business", created_by_user_id=uid)
    cid = cid["id"] if isinstance(cid, dict) else cid
    C.add_address(int(cid), kind="work", line1="Lindenallee 4", postcode="31224", city="Peine", country="DE")

    out = asyncio.run(execute(_ctx(uid), recipient="Hausverwaltung Müller", subject="Heizung defekt",
                              text="Sehr geehrte Damen und Herren,\n\ndie Heizung ist seit Montag kalt.\n\nMit freundlichen Grüßen\nBeate"))
    assert out["missing"] == [] and "do not ask" not in out["_llm_hint"]
    doc = client.get(f"/api/writing/{out['document_id']}").json()
    assert doc["status"] == "draft" and doc["kind"] == "letter" and doc["title"] == "Heizung defekt"
    assert doc["recipient"]["address_lines"] == ["Lindenallee 4", "31224 Peine"] and doc["recipient"]["contact_id"] == int(cid)
    assert doc["content"]["text_html"].startswith("<p>Sehr geehrte Damen und Herren,</p><p>die Heizung")
    card = [a for a in ui_tools.get_ui_actions() if a.get("type") == "writing_draft_created"] if hasattr(ui_tools, "get_ui_actions") else []
    assert not card or card[-1]["document_id"] == out["document_id"]

    # somebody the contacts do not know: a draft all the same, the address marked on the sheet
    out2 = asyncio.run(execute(_ctx(uid), recipient="Finanzamt Peine", subject="Einspruch", text="Sehr geehrte Damen und Herren,\n\nich lege Einspruch ein."))
    assert out2["missing"] == ["Adresse"] and "do not ask" in out2["_llm_hint"]
    assert "Adresse fehlt" in client.get(f"/api/writing/{out2['document_id']}/preview").json()["html"]
    # a change to the same draft, not a second one
    out3 = asyncio.run(execute(_ctx(uid), recipient="Finanzamt Peine", subject="Einspruch gegen den Bescheid", text="Neu.", document_id=out2["document_id"]))
    assert out3["document_id"] == out2["document_id"] and len(client.get("/api/writing").json()["documents"]) == 2
    with pytest.raises(ValueError):
        asyncio.run(execute(_ctx(uid), recipient="x", subject="y", text="  "))


def test_a_letter_is_yours_is_edited_and_becomes_final_when_it_leaves(fresh_app, pdf_service, tmp_path, monkeypatch):
    monkeypatch.setenv("YORIK_WRITTEN_DIR", str(tmp_path / "written"))
    from backend import main as M
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    dirk_c, _ = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    kid_c, _ = login_client(fresh_app, role="restricted", name="Yorik", email="k@example.local")

    assert beate_c.get("/api/writing").json() == {"documents": [], "kinds": ["letter"]}
    assert beate_c.post("/api/writing", json={"kind": "invoice"}).status_code == 403          # stage 3
    assert beate_c.post("/api/writing", json={"kind": "poster"}).status_code == 400
    assert kid_c.post("/api/writing", json={"kind": "letter", "content": {"text": "Liebe Oma"}}).status_code == 201

    r = beate_c.post("/api/writing", json={"kind": "letter", "recipient": {"name": "Stadtwerke", "address_lines": "Am Markt 1\n31224 Peine", "email": "kunden@stadtwerke.example"},
                                           "content": {"subject": "Zählerstand", "text_html": '<p onclick="x()">Guten Tag</p><script>1</script>'}})
    assert r.status_code == 201, r.text
    doc = r.json(); did = doc["id"]
    assert doc["title"] == "Zählerstand" and doc["recipient"]["address_lines"] == ["Am Markt 1", "31224 Peine"]
    assert "onclick" not in doc["content"]["text_html"] and "script" not in doc["content"]["text_html"]
    for other in (dirk_c, kid_c):                                                              # no admin exception for seeing
        assert other.get(f"/api/writing/{did}").status_code == 404
        assert other.get(f"/api/writing/{did}/pdf").status_code == 404
        assert other.patch(f"/api/writing/{did}", json={"title": "x"}).status_code == 404
    assert beate_c.patch(f"/api/writing/{did}", json={"content": {"subject": "Zählerstand 2026", "text_html": "<p>Neu</p>"}}).json()["content"]["subject"] == "Zählerstand 2026"

    pdf = beate_c.get(f"/api/writing/{did}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and "filename*=UTF-8''Z%C3%A4hlerstand" in pdf.headers["content-disposition"] and set(pdf_service[-1][0]) == {"index.html", "footer.html"}
    assert beate_c.get(f"/api/writing/{did}").json()["status"] == "draft"                       # looking does not finalise

    # filing: through her own token, and then it is final
    pushed = {}
    monkeypatch.setattr(M, "_push_to_paperless", lambda blob, **kw: (pushed.update(kw, size=len(blob)), {"ok": True, "task_id": None})[1])
    filed = beate_c.post(f"/api/writing/{did}/paperless", json={"visibility": "private"})
    assert filed.status_code == 200, filed.text
    assert pushed["user_id"] == beate and pushed["visibility"] == "private" and pushed["filename"] == "Zählerstand 2026.pdf"
    final = beate_c.get(f"/api/writing/{did}").json()
    assert final["status"] == "final" and final["has_pdf"] and final["finalised_at"]
    assert beate_c.patch(f"/api/writing/{did}", json={"title": "später"}).status_code == 409
    assert beate_c.delete(f"/api/writing/{did}").status_code == 409
    n = len(pdf_service); assert beate_c.get(f"/api/writing/{did}/pdf").status_code == 200 and len(pdf_service) == n    # the kept PDF, not a new render
    copy = beate_c.post(f"/api/writing/{did}/duplicate").json()
    assert copy["status"] == "draft" and copy["id"] != did and copy["content"]["subject"] == "Zählerstand 2026"
    assert beate_c.delete(f"/api/writing/{copy['id']}").status_code == 204


def test_sending_uses_your_own_mail_account_only(fresh_app, pdf_service, tmp_path, monkeypatch):
    monkeypatch.setenv("YORIK_WRITTEN_DIR", str(tmp_path / "written"))
    from backend import email_sender
    from backend.database import get_conn
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    _, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    with get_conn() as conn:
        cols = [r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'email_accounts' AND is_nullable = 'NO' AND column_default IS NULL").fetchall()]
        def account(owner, mail):
            vals = {"owner_user_id": owner, "email": mail, "imap_host": "i", "imap_port": 993, "imap_username": mail, "smtp_host": "s", "smtp_port": 465,
                    "smtp_username": mail, "credential_key": f"k-{mail}"}
            use = {k: v for k, v in vals.items() if k in cols or k in ("owner_user_id", "email")}
            return conn.execute(f"INSERT INTO email_accounts ({', '.join(use)}) VALUES ({', '.join('?' * len(use))}) RETURNING id", tuple(use.values())).fetchone()["id"]
        hers, his = account(beate, "beate@example.local"), account(dirk, "dirk@example.local")
        conn.commit()
    did = beate_c.post("/api/writing", json={"kind": "letter", "content": {"subject": "Kündigung", "text": "Hiermit kündige ich."}}).json()["id"]
    sent = {}
    monkeypatch.setattr(email_sender, "send", lambda account_id, to, subject, body_text, **kw: (sent.update(account_id=account_id, to=to, subject=subject, att=kw["attachments"]), {"ok": True})[1])
    assert beate_c.post(f"/api/writing/{did}/send", json={"account_id": his, "to": "a@b.example", "subject": "x"}).status_code == 404
    assert beate_c.post(f"/api/writing/{did}/send", json={"account_id": hers, "to": "niemand", "subject": "x"}).status_code == 400
    assert sent == {} and beate_c.get(f"/api/writing/{did}").json()["status"] == "draft"
    r = beate_c.post(f"/api/writing/{did}/send", json={"account_id": hers, "to": "service@mobil.example; zweite@mobil.example", "subject": ""})
    assert r.status_code == 200, r.text
    assert sent["account_id"] == hers and sent["to"] == ["service@mobil.example", "zweite@mobil.example"] and sent["subject"] == "Kündigung"
    assert sent["att"][0]["filename"] == "Kündigung.pdf" and sent["att"][0]["content"].startswith(b"%PDF")
    assert r.json()["document"]["status"] == "final"


def test_while_schreiben_is_on_the_old_compose_skills_rest(fresh_app, monkeypatch):
    from backend.skills import registry as R
    monkeypatch.delenv("YORIK_ENABLE_WRITE", raising=False)
    off = R._skills_of_disabled_apps()
    assert "write_letter" in off and "compose_draft" not in off
    monkeypatch.setenv("YORIK_ENABLE_WRITE", "1")
    on = R._skills_of_disabled_apps()
    assert "write_letter" not in on and {"compose_draft", "pick_compose_template"} <= on and "email_draft" not in on


def test_rewrite_hands_text_to_the_model_and_text_back(fresh_app, monkeypatch):
    from backend.agent import llm
    seen = {}

    class _Fake:
        def __init__(self, **kw): pass
        def chat(self, messages, **kw):
            seen["prompt"] = messages[0]["content"]
            return {"content": '„Die Heizung ist leider seit Montag kalt.“'}

    monkeypatch.setattr(llm, "LlmClient", _Fake)
    client, _ = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    r = client.post("/api/writing/rewrite", json={"text": "Heizung kalt seit Montag.", "instruction": "freundlicher"})
    assert r.status_code == 200 and r.json()["text"] == "Die Heizung ist leider seit Montag kalt."
    assert "freundlicher" in seen["prompt"] and "Heizung kalt" in seen["prompt"]
    assert client.post("/api/writing/rewrite", json={"text": " ", "instruction": "x"}).status_code == 400
