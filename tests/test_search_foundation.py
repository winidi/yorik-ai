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


def test_timestamps_are_local_with_offset():
    from backend.search_routes import _local
    assert _local("2026-09-27 10:07:57") == "2026-09-27T12:07+02:00"
    assert _local(1790000000).endswith("+02:00") and _local("2026-09-27") == "2026-09-27"


def test_search_for_model_keeps_every_source_and_puts_title_hits_first():
    """Rerun 2026-09-27: twenty long mail/WhatsApp hits pushed the
    recording "Regeln für Yorik" past the 6000-character cut."""
    from backend.agent.prefetch import for_model
    long = "x" * 900
    raw = {"query": "regeln yorik", "total": 21, "results": {
        "email": [{"source": "email", "id": i, "title": f"Mail {i}", "snippet": long} for i in range(10)],
        "whatsapp": [{"source": "whatsapp", "id": i, "title": "Chat", "snippet": long} for i in range(10)],
        "recordings": [{"source": "recordings", "id": 8, "title": "Regeln für Yorik", "thumbnail_url": None}],
    }}
    out = for_model(raw, "besprochen regeln yorik")
    assert list(out["results"])[0] == "recordings"
    assert out["results"]["recordings"][0]["source"] == "recordings"
    assert all(len(h) <= 3 for h in out["results"].values())
    assert all(len(h.get("snippet", "")) <= 160 for v in out["results"].values() for h in v)
    assert "14 further hits" in out["more"]


def test_references_header_is_split_into_ids():
    """The fetcher stored "<a@x> <b@y>" as single characters (2026-09-27)."""
    from backend.email_fetcher import message_ids
    assert message_ids("<a@x.de>\r\n <b.1@y.com>") == ["a@x.de", "b.1@y.com"]
    assert message_ids(["<a@x.de>", "<b@y>"]) == ["a@x.de", "b@y"]
    assert message_ids("a@x.de b@y") == ["a@x.de", "b@y"]
    assert message_ids(None) == []


def test_paperless_documents_are_in_the_shared_index_and_seen_as_the_person(house, monkeypatch):
    """Chat rerun 2026-09-27: the English netcup invoice sat only in the
    small English MiniLM mirror and a German question never reached it.
    Documents now share the index; the person's token still decides."""
    from backend import paperless_ingest as PI, search_index
    from backend.database import get_conn
    with get_conn() as conn:
        conn.execute("INSERT INTO docs.paperless_documents (id, title, correspondent, content) VALUES "
                     "(1, 'Your invoice nc-5276888', 'netcup GmbH', 'RS 4000 server, invoice amount 551,07 EUR'), "
                     "(2, 'Projektvertrag', 'Kommpact', 'Kündigung nur schriftlich')")
        conn.commit()
    search_index.sweep()
    with get_conn() as conn:
        first = conn.execute("SELECT text FROM search_chunks WHERE source = 'paperless' AND row_id = 1").fetchone()
    assert first["text"].startswith("Your invoice nc-5276888\nnetcup GmbH")

    seen = []
    def get(url, params=None, **kw):
        seen.append(kw["headers"]["Authorization"])
        if "fields=id" in url:                                   # what she may see
            return _Resp([{"id": 1}])
        ids = [int(i) for i in (params or {}).get("id__in", "").split(",") if i]
        return _Resp([{"id": i, "title": f"doc {i}", "tags": []} for i in ids if i != 2])   # 2 is not hers
    monkeypatch.setattr(PI.requests, "get", get)
    PI._VISIBLE.clear()
    hits = PI.search("was kostet der server", k=5, creds_override={"base_url": "http://p", "api_key": "beate"})
    assert [h["paperless_doc_id"] for h in hits] == [1]
    assert set(seen) == {"Token beate"}


def test_a_member_finds_her_one_document_among_many_of_the_admin(house, monkeypatch):
    """Ranking the household first and filtering afterwards lost a
    member's document once the admin's filled the top (2026-09-28)."""
    from backend import paperless_ingest as PI, search_index
    from backend.database import get_conn
    with get_conn() as conn:
        for i in range(1, 41):                                    # forty server invoices of the admin
            conn.execute("INSERT INTO docs.paperless_documents (id, title, content) VALUES (?, ?, ?)",
                         (i, f"Server invoice {i}", "server invoice hosting"))
        conn.execute("INSERT INTO docs.paperless_documents (id, title, content) VALUES "
                     "(99, 'Mein Vertrag', 'server Vertrag von Beate')")
        conn.commit()
    search_index.sweep()

    def get(url, params=None, **kw):
        if "fields=id" in url:
            return _Resp([{"id": 99}])                            # hers only
        ids = [int(i) for i in (params or {}).get("id__in", "").split(",") if i]
        return _Resp([{"id": i, "title": f"doc {i}", "tags": []} for i in ids if i == 99])
    monkeypatch.setattr(PI.requests, "get", get)
    PI._VISIBLE.clear()
    hits = PI.search("server", k=5, creds_override={"base_url": "http://p", "api_key": "beate2"})
    assert [h["paperless_doc_id"] for h in hits] == [99]


class _Resp:
    ok, status_code = True, 200
    def __init__(self, results):
        self._r = results
    def raise_for_status(self):
        pass
    def json(self):
        return {"results": self._r, "count": len(self._r)}


def test_ingest_writes_and_removes_the_document_row(house, monkeypatch):
    from backend import paperless_ingest as PI
    from backend.database import get_conn
    monkeypatch.setattr(PI, "_fetch_doc", lambda i, creds_override=None: {
        "id": i, "title": "Your invoice", "correspondent": 7, "document_type": None,
        "created_date": "2026-06-18", "content": "RS 4000 invoice amount 551,07 EUR", "tags": []})
    monkeypatch.setattr(PI, "_apply_space_marker", lambda *a: None)
    monkeypatch.setattr(PI, "_name_of", lambda kind, ref: "netcup GmbH" if ref == 7 else "")
    monkeypatch.setattr(PI, "embed", lambda text: [1.0] + [0.0] * 383)
    assert PI.ingest_one(1)["ok"]
    with get_conn() as conn:
        row = conn.execute("SELECT title, correspondent, doc_date FROM docs.paperless_documents WHERE id = 1").fetchone()
    assert (row["title"], row["correspondent"], row["doc_date"]) == ("Your invoice", "netcup GmbH", "2026-06-18")
    assert PI._mirrored_ids() == {1}
    assert PI.ingest_one(1)["ok"]                                    # re-ingest replaces, no duplicate key
    from backend.database_pg import conn_ctx_pg
    with conn_ctx_pg("docs") as conn:
        PI._delete_existing_chunks(conn, 1)
    assert PI._mirrored_ids() == set()


def test_universal_search_runs_the_models_other_wordings(house, monkeypatch):
    """"Claude" never reached the Anthropic receipt; the model adds
    "Anthropic" as another wording and both result sets merge."""
    import asyncio
    from backend import search_routes
    from backend.skills.registry import Registry, SkillContext
    from backend.skills.universal_search.skill import execute
    asked = []
    async def fake(q, user):
        asked.append(q)
        hits = {"claude bezahlt": [{"source": "bank", "id": 1, "title": "CLAUDE SUB"}],
                "anthropic receipt": [{"source": "bank", "id": 1, "title": "CLAUDE SUB"}]}.get(q, [])
        docs = [{"source": "paperless", "id": 7, "title": "Your receipt from Anthropic"}] if "anthropic" in q else []
        return {"query": q, "total": len(hits) + len(docs), "results": {"bank": hits, "paperless": docs}}
    monkeypatch.setattr(search_routes, "universal_search", fake)
    _, dirk = house["dirk"]
    ctx = SkillContext(Registry(), role="platform_admin", user_id=dirk)
    out = asyncio.run(execute(ctx, "claude bezahlt", also=["anthropic receipt", "Claude bezahlt", " "]))
    assert asked == ["claude bezahlt", "anthropic receipt"]
    assert [h["id"] for h in out["results"]["bank"]] == [1]                 # no duplicate
    assert out["results"]["paperless"][0]["id"] == 7
    assert out["also_searched"] == ["anthropic receipt"]


def test_prefetch_searches_the_models_variants_too(house, monkeypatch):
    """Dirk 2026-09-27: a short model call before the search supplies
    other wordings ("claude" → "anthropic invoice")."""
    import asyncio, json
    from backend import search_routes
    from backend.agent import prefetch
    asked = []
    async def fake_variants(message, query):
        return ["anthropic invoice"]
    async def fake_search(q, user):
        asked.append(q)
        docs = [{"source": "paperless", "id": 7, "title": "Your receipt from Anthropic"}] if "anthropic" in q else []
        bank = [{"source": "bank", "id": 1, "title": "ANTHROPIC CLAUDE SUB"}]
        return {"query": q, "total": 1 + len(docs), "results": {"bank": bank, "paperless": docs}}
    monkeypatch.setattr(prefetch, "variants", fake_variants)
    monkeypatch.setattr(search_routes, "universal_search", fake_search)
    _, dirk = house["dirk"]
    out = asyncio.run(prefetch.run("was hab ich für claude bezahlt", user_id=dirk, role="platform_admin"))
    assert asked == ["claude bezahlt", "anthropic invoice"]
    assert [h["id"] for h in out["raw"]["results"]["bank"]] == [1]
    assert out["raw"]["results"]["paperless"][0]["id"] == 7
    args = json.loads(out["messages"][0]["tool_calls"][0]["function"]["arguments"])["args"]
    assert args == {"query": "claude bezahlt", "also": ["anthropic invoice"]}


def test_variants_parse_the_models_json_and_survive_nonsense(monkeypatch):
    import asyncio
    import httpx
    from backend.agent import prefetch
    monkeypatch.undo()                     # the real variants(), not the conftest stub
    class _R:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": self.text}}]}
    answers = iter(['Sure! {"also": ["anthropic invoice", "Claude Bezahlt", "x", "y", "z"]}', "no json here"])
    class _Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return _R(next(answers))
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert asyncio.run(prefetch.variants("was hab ich für claude bezahlt", "claude bezahlt")) == ["anthropic invoice", "x", "y"]
    assert asyncio.run(prefetch.variants("q", "q")) == []
