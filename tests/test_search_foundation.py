"""Search foundation after the chat test on real data (2026-09-26):
WhatsApp hits carry who/where, bare values (an IBAN) carry the words a
person searches for, bank bookings and Schreiben documents are
searchable, broken mail umlauts are repaired."""

from __future__ import annotations

import pytest

from tests.conftest import login_client

AXES = (("iban", "kontonummer"), ("mama",), ("hetzner", "server"), ("kündigung", "kobra"))


def _embed(texts):
    out = []
    for t in texts:
        low = t.lower()
        v = [0.0] * 384
        for i, words in enumerate(AXES):
            if any(w in low for w in words):
                v[i] = 1.0
        if not any(v):
            v[20] = 1.0
        n = sum(x * x for x in v) ** 0.5
        out.append([x / n for x in v])
    return out


@pytest.fixture
def house(fresh_app, monkeypatch):
    from backend import search_index, spaces as S
    monkeypatch.setattr(search_index, "embed_many", _embed)
    monkeypatch.setattr(search_index, "EMBED_URL", "")
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    S.ensure_workspace_exists(dirk, "Dirk")
    for uid, name in ((dirk, "Dirk"), (beate, "Beate")):
        S.ensure_personal_space(uid, name)
    return {"dirk": (dirk_c, dirk), "beate": (beate_c, beate)}


def _results(client, q):
    r = client.get("/api/search", params={"q": q})
    assert r.status_code == 200, r.text
    return r.json()["results"]


def test_whatsapp_iban_found_by_words_and_chat_name(house):
    from backend import search_index
    from backend.database import get_conn
    dirk_c, dirk = house["dirk"]
    jid = "491770000000@s.whatsapp.net"
    with get_conn() as conn:
        conn.execute("INSERT INTO wa_chats (jid, name, is_group, owner_user_id) VALUES (?, 'Mama Nowa', 0, ?)",
                     (jid, dirk))
        conn.execute("INSERT INTO wa_messages (msg_id, chat_jid, from_me, push_name, timestamp, text, owner_user_id) "
                     "VALUES ('m1', ?, 0, 'Mama', 1790000000, 'DE85500105175438012374', ?)", (jid, dirk))
        conn.commit()
    search_index.sweep()
    with get_conn() as conn:
        chunk = conn.execute("SELECT text, content_hash FROM search_chunks WHERE source = 'whatsapp'").fetchone()
    assert chunk["text"].startswith("WhatsApp-Chat Mama Nowa · Mama ·")
    assert "IBAN Kontonummer Bankverbindung" in chunk["text"]
    assert chunk["content_hash"].startswith("v2:")
    hits = _results(dirk_c, "Kontonummer von Mama")["whatsapp"]
    assert [h["snippet"] for h in hits] == ["DE85500105175438012374"]
    assert hits[0]["chat_jid"] == jid


def test_old_whatsapp_chunks_are_rewritten_after_the_version_bump(house):
    from backend import search_index
    from backend.database import get_conn
    _, dirk = house["dirk"]
    jid = "491770000000@s.whatsapp.net"
    with get_conn() as conn:
        conn.execute("INSERT INTO wa_chats (jid, name, is_group, owner_user_id) VALUES (?, 'Mama Nowa', 0, ?)",
                     (jid, dirk))
        row = conn.execute("INSERT INTO wa_messages (msg_id, chat_jid, from_me, timestamp, text, owner_user_id) "
                           "VALUES ('m1', ?, 0, 1790000000, 'Bring bitte Brot mit', ?) RETURNING id",
                           (jid, dirk)).fetchone()
        # a chunk the way the old indexer wrote it: bare text, plain hash
        conn.execute("INSERT INTO search_chunks (source, row_id, chunk_no, text, content_hash, embedding, model, indexed_at) "
                     "VALUES ('whatsapp', ?, 0, 'Bring bitte Brot mit', 'abc', NULL, ?, '2026-09-20T10:00:00')",
                     (row["id"], search_index.model_tag()))
        conn.commit()
    search_index.sweep()
    with get_conn() as conn:
        texts = [r["text"] for r in conn.execute("SELECT text FROM search_chunks WHERE source = 'whatsapp'")]
    assert texts and texts[0].startswith("WhatsApp-Chat Mama Nowa")


def _account(owner, space_id=None):
    from backend.database import get_conn
    with get_conn() as conn:
        acc = int(conn.execute(
            "INSERT INTO bank_accounts (owner_user_id, space_id, display_name, bank_url, blz, login_name, credential_key) "
            "VALUES (?, ?, 'Konto', 'https://example.invalid', '0', 'x', 'u') RETURNING id", (owner, space_id)
        ).fetchone()["id"])
        conn.commit()
    return acc


def test_bank_bookings_searchable_only_for_whom_may_see_them(house):
    from backend import search_index
    from backend.database import get_conn
    dirk_c, dirk = house["dirk"]; beate_c, beate = house["beate"]
    mine, hers = _account(dirk), _account(beate)
    with get_conn() as conn:
        for acc, cp, h in ((mine, "VISA HETZNER ONLINE GMBH", "a"), (hers, "Hetzner Cloud privat", "b")):
            conn.execute("INSERT INTO bank_transactions (account_id, booking_date, amount, counterparty, purpose, "
                         "category, dedup_hash) VALUES (?, '2026-09-08', -59.49, ?, 'Server', 'Verträge & Abos', ?)",
                         (acc, cp, h))
        conn.commit()
    search_index.sweep()
    assert [h["title"] for h in _results(dirk_c, "hetzner")["bank"]] == ["VISA HETZNER ONLINE GMBH"]
    assert [h["title"] for h in _results(beate_c, "hetzner")["bank"]] == ["Hetzner Cloud privat"]


def test_letters_searchable_by_their_owner(house):
    import json
    from backend import search_index
    from backend.database import get_conn
    dirk_c, dirk = house["dirk"]; beate_c, _ = house["beate"]
    with get_conn() as conn:
        conn.execute("INSERT INTO written_documents (user_id, kind, status, title, recipient, content) "
                     "VALUES (?, 'letter', 'draft', 'Kündigung Mitgliedschaft', ?, ?)",
                     (dirk, json.dumps({"name": "Kobra Kampfsport"}),
                      json.dumps({"subject": "Kündigung", "text_html": "<p>hiermit kündige ich</p>"})))
        conn.commit()
    search_index.sweep()
    assert [h["title"] for h in _results(dirk_c, "kündigung")["letters"]] == ["Kündigung Mitgliedschaft"]
    assert _results(beate_c, "kündigung")["letters"] == []


def test_mail_umlauts_repaired_on_import_and_in_store(house):
    from backend import email_fetcher as ef
    from backend.database import get_conn
    assert ef.fix_mojibake("Styles fÃ¼r die Ã\x9cbergangszeit, 5 â‚¬") == "Styles für die Übergangszeit, 5 €"
    assert ef.fix_mojibake("Straße und château") == "Straße und château"
    _, dirk = house["dirk"]
    with get_conn() as conn:
        acc = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                           "smtp_username, credential_key) VALUES (?, 'd@example.local', 'i', 'u', 's', 'u', 'k') "
                           "RETURNING id", (dirk,)).fetchone()["id"]
        mid = conn.execute("INSERT INTO email_messages (account_id, uid, subject, body_text, owner_user_id) "
                           "VALUES (?, 1, 'Hallo', 'kÃ¶nnen Sie', ?) RETURNING id", (acc, dirk)).fetchone()["id"]
        conn.commit()
    assert ef.repair_stored_mojibake() == 1
    with get_conn() as conn:
        assert conn.execute("SELECT body_text FROM email_messages WHERE id = ?", (mid,)).fetchone()["body_text"] == "können Sie"
    assert ef.repair_stored_mojibake() == 0


def _mail_row(conn, acc, uid, n, subject, body, sender, date, category=None):
    conn.execute("INSERT INTO email_messages (account_id, uid, subject, body_text, snippet, from_email, from_name, "
                 "date_received, category, owner_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (acc, n, subject, body, body[:100], sender, sender.split("@")[0], date, category, uid))


def test_mail_relevance_beats_date_and_newsletters_sink(house):
    from backend.database import get_conn
    dirk_c, dirk = house["dirk"]
    with get_conn() as conn:
        acc = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                           "smtp_username, credential_key) VALUES (?, 'd@example.local', 'i', 'u', 's', 'u', 'k') "
                           "RETURNING id", (dirk,)).fetchone()["id"]
        _mail_row(conn, acc, dirk, 1, "[GitHub] Payment Receipt", "We received payment for your GitHub subscription",
                  "noreply@github.com", "2026-06-01T10:00:00+00:00")
        for i in range(6):
            _mail_row(conn, acc, dirk, 10 + i, f"Daily Digest {i}", "read more on github.com/some/repo",
                      "digest@medium.com", f"2026-09-2{i}T10:00:00+00:00", "newsletter")
        conn.commit()
    hits = _results(dirk_c, "github bezahlt")["email"]
    assert hits[0]["title"] == "[GitHub] Payment Receipt"


def test_paperless_keyword_leg_finds_what_meaning_missed(house, monkeypatch):
    from backend import paperless_ingest, search_routes, external_users
    dirk_c, dirk = house["dirk"]
    monkeypatch.setattr(external_users, "get_user_paperless_creds", lambda uid: {"api_key": "k", "base_url": "http://x"})
    monkeypatch.setattr(paperless_ingest, "search", lambda q, k, creds_override=None: [])
    seen = {}

    def fts(q, k, creds_override=None):
        seen["q"] = q
        return [{"paperless_doc_id": 2, "doc_title": "[GitHub] Payment Receipt for winidi", "text": "GitHub Developer Plan $4",
                 "doc_date": "2026-06-01", "distance": None, "match_type": "fts"}]
    monkeypatch.setattr(paperless_ingest, "search_fts", fts)
    hits = search_routes._search_paperless("dieses github ding zahlen", dirk)
    assert [h["title"] for h in hits] == ["[GitHub] Payment Receipt for winidi"]
    assert seen["q"] == "dieses OR github OR ding OR zahlen"


def test_rounded_bold_amount_is_not_checked():
    from backend.agent.grounding import check
    assert check("Das sind also rund **75** Euro pro Monat.", [], []).ok
