"""The search's ranking pieces that need no database: fusing the word
and meaning lists, folding twins, reading the time scope off a question,
readable text from HTML mail, the reranker's candidate choice.
Measured counterpart: ~/yorikai/search-eval (private, real data)."""

from __future__ import annotations

from datetime import date

import pytest

from backend import search_rerank
from backend import search_routes as sr
from backend.agent import prefetch
from backend.email_text import html_to_text, looks_like_markup


# ─── fusion ─────────────────────────────────────────────────────────

def test_rrf_lets_a_second_meaning_hit_through():
    """Interleaving showed word 1, meaning 1, word 2 — the second hit by
    meaning never reached a three-row view. Fused, it sits third."""
    words = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
    meaning = [{"id": 10, "_snippet": "a"}, {"id": 11, "_snippet": "b"}, {"id": 1}]
    fused = sr._rrf([(words, 1.0), (meaning, 1.0)])
    ids = [r["id"] for r in fused]
    assert ids[0] == 1                       # in both lists
    assert 11 in ids[:4]                     # second meaning hit is visible
    assert fused[0]["_score"] > fused[1]["_score"]


def test_rrf_keeps_the_meaning_snippet():
    fused = sr._rrf([([{"id": 1, "snippet": "x"}], 1.0), ([{"id": 1, "_snippet": "the passage"}], 1.0)])
    assert fused[0]["_snippet"] == "the passage"


def test_collapse_counts_twins_and_keeps_the_first():
    rows = [{"id": 1, "subject": "Erinnerung: Mit Docusign abschließen: 2026-08-31 x.pdf", "from_email": "d@x", "snippet": "Guten Tag"},
            {"id": 2, "subject": "Re: Mit Docusign abschließen: 2026-09-02 x.pdf", "from_email": "d@x", "snippet": "Guten Tag"},
            {"id": 3, "subject": "Mit Docusign abschließen: 2026-09-02 x.pdf", "from_email": "d@x", "snippet": "Hey Dirk, was meinst du"},
            {"id": 4, "subject": "Other", "from_email": "o@x", "snippet": ""}]
    out = sr._collapse(rows, sr._email_twin)
    assert [r["id"] for r in out] == [1, 3, 4]       # 2 is 1 again; 3 is a reply with its own text
    assert out[0]["_twins"] == 1


def test_merge_prefers_the_best_single_vote_over_many_weak_ones():
    """Three paraphrases agreeing on a newsletter at place 3 must not
    beat the user's own words putting the right mail first."""
    own = {"results": {"email": [{"source": "email", "id": "right", "title": "Re: GoHighLevel", "subtitle": "Oliver"},
                                 {"source": "email", "id": "n1", "title": "News 1", "subtitle": "Shop"},
                                 {"source": "email", "id": "n2", "title": "News 2", "subtitle": "Shop"}]}}
    para = {"results": {"email": [{"source": "email", "id": "n1", "title": "News 1", "subtitle": "Shop"},
                                  {"source": "email", "id": "n2", "title": "News 2", "subtitle": "Shop"},
                                  {"source": "email", "id": "n3", "title": "News 3", "subtitle": "Shop"}]}}
    merged = prefetch.merge([own, para, dict(para), dict(para)])
    assert merged["results"]["email"][0]["id"] == "right"


def test_merge_folds_the_same_mail_sent_six_times():
    hits = [{"source": "email", "id": i, "title": "Erinnerung: Mit Docusign abschließen: 2026-08-31 x.pdf",
             "subtitle": "Docusign", "snippet": "Guten Tag, Dirk"} for i in range(6)]
    hits.append({"source": "email", "id": 99, "title": "Rechnung", "subtitle": "netcup", "snippet": "Ihre Rechnung"})
    merged = prefetch.merge([{"results": {"email": hits}}])
    out = merged["results"]["email"]
    assert len(out) == 2 and out[0]["duplicates"] == 5 and merged["total"] == 2


def test_for_model_shows_five_and_hides_internals():
    raw = {"results": {"email": [{"source": "email", "id": i, "title": f"m{i}", "_score": 1.0 / (i + 1), "_rerank": 0.1}
                                 for i in range(8)]}, "total": 8}
    view = prefetch.for_model(raw, "m1")
    assert len(view["results"]["email"]) == 5
    assert "_score" not in view["results"]["email"][0] and "_rerank" not in view["results"]["email"][0]
    assert "3 further hits" in view["more"]


def test_for_model_keeps_the_reranked_source_order():
    raw = {"results": {"tasks": [{"id": 1, "title": "x"}], "email": [{"id": 2, "title": "x"}]}, "total": 2, "reranked": 2}
    assert list(prefetch.for_model(raw, "nothing")["results"]) == ["tasks", "email"]
    assert "reranked" not in prefetch.for_model(raw, "nothing")


# ─── the question's words and time ──────────────────────────────────

def test_keywords_drop_chat_verbs():
    assert prefetch.keywords("erinnerst du dich an die mail von grok, darum ging es um die drei wichtigsten") == "grok drei wichtigsten"
    assert prefetch.keywords("was hat jan wegen hansefit geschrieben?") == "jan hansefit"
    assert prefetch.keywords("riverty, muss ich da jetzt noch was bezahlen oder nich") == "riverty bezahlen"


@pytest.mark.parametrize("q, want", [
    ("die Überweisung vom 2.7.", {"date_from": "2026-07-02", "date_to": "2026-07-02"}),
    ("was kam gestern von beate", {"date_from": "2026-10-04", "date_to": "2026-10-04"}),
    ("letzte woche mails von jan", {"date_from": "2026-09-28", "date_to": "2026-10-04", "recent": True}),
    ("die letzten mails von beate mayer", {"recent": True}),
    ("im september die ki rechnung", {"date_from": "2026-09-01", "date_to": "2026-09-30"}),
    ("letzten monat hetzner abgebucht", {"date_from": "2026-09-01", "date_to": "2026-09-30", "recent": True}),
    ("beate hat mir nen termin am 1.11. um 14 uhr geschickt", {}),      # a day to come is the content
    ("was hat jan wegen hansefit geschrieben?", {}),
    ("hat mai was geschrieben", {}),                                     # a name, not the month
])
def test_time_scope(q, want):
    assert prefetch.time_scope(q, today=date(2026, 10, 5)) == want


def test_tsquery_recent_keeps_only_the_rarest_word():
    words = [("letzten", 0.3), ("beate", 2.1), ("mayer", 4.7)]
    assert sr._tsquery(words, recent=False) == "letzten:* | beate:* | mayer:*"
    assert sr._tsquery(words, recent=True) == "mayer:*"
    assert sr._tsquery([], recent=False) is None


def test_weight_sql_one_case_per_word():
    sql, params = sr._weight_sql("t.search_tsv", [("github", 5.2), ("rechnung", 1.1)])
    assert sql.count("CASE WHEN") == 2 and params == ["github:*", "rechnung:*"]
    assert sr._weight_sql("t.search_tsv", []) == ("0", [])


def test_date_filter_sql_reads_iso_and_epoch():
    sql, params = sr._date_filter("m.date_received", "2026-07-01", None)
    assert "to_timestamp" in sql and "substr" in sql and params == ["2026-07-01", "9999-12-31"]
    assert sr._date_filter("m.x", None, None) == ("TRUE", [])


# ─── mail text ──────────────────────────────────────────────────────

HTML = """<!DOCTYPE html><html><head><title>Snuzone</title>
<style>/* What it does: Remove spaces */ table{border-collapse:collapse} .ExternalClass{width:100%}</style>
<!--[if mso]><xml><o:OfficeDocumentSettings/></xml><![endif]--></head>
<body style="margin:0"><table><tr><td><h1>BESTELLUNG BESTÄTIGT!</h1></td></tr>
<tr><td><p>Hey Dirk,</p><p>wir bereiten deine <b>Bestellung</b> f&uuml;r den Versand vor.</p>
<a href="https://track.example.com/x?y=1">Sendung verfolgen</a><script>var x = 1;</script></td></tr></table>
<div>&#847;&#8204;&#847;&#8204; Preheader</div></body></html>"""


def test_html_to_text_keeps_words_and_drops_styles():
    text = html_to_text(HTML)
    assert "BESTELLUNG BESTÄTIGT!" in text and "für den Versand" in text and "Sendung verfolgen" in text
    assert "border-collapse" not in text and "ExternalClass" not in text and "mso" not in text
    assert "var x" not in text and "track.example.com" not in text and "Snuzone" not in text   # <title> is no body
    assert "\n" in text                                     # blocks keep their breaks
    assert not looks_like_markup(text)
    assert looks_like_markup("/* What it does */ table{x:y;}")
    assert html_to_text("") == "" and html_to_text("plain &amp; simple") == "plain & simple"


def test_html_to_text_survives_broken_markup():
    assert "hello" in html_to_text("<div><p>hello<span></div>")
    assert html_to_text("<style>a{b:c}</style>") == ""


# ─── reranker ───────────────────────────────────────────────────────

def test_rerank_is_a_no_op_when_off(monkeypatch):
    monkeypatch.setattr(search_rerank, "RERANK_URL", "")
    raw = {"results": {"email": [{"id": 1, "title": "a"}]}}
    assert search_rerank.rerank("q", raw) is raw


def test_rerank_orders_within_a_source_and_sources_by_best(monkeypatch):
    monkeypatch.setattr(search_rerank, "RERANK_URL", "http://x")
    raw = {"results": {
        "tasks": [{"id": "t1", "title": "Uhr dran machen"}],
        "email": [{"id": "e1", "title": "Bestellt: Thermalright", "snippet": "x"},
                  {"id": "e2", "title": "Bestellung wurde zugestellt", "subtitle": "Snuzone", "snippet": "Dein Paket"},
                  {"id": "e3", "title": "Neujahrsvorsatz", "snippet": "y"}],
        "immich": [{"id": "p1", "title": "PXL.jpg"}]}}
    captured = {}

    def fake(query, docs, timeout):
        captured["docs"] = docs
        return [-11.0, -8.0, -12.0, -13.0][:len(docs)]        # e1, e2, e3 (deep source first), then tasks
    monkeypatch.setattr(search_rerank, "_scores", fake)
    out = search_rerank.rerank("wo hab ich snooze bestellt", raw)
    assert len(captured["docs"]) == 4                        # photos are not read
    assert [h["id"] for h in out["results"]["email"]] == ["e2", "e1", "e3"]
    assert list(out["results"]) [0] == "email" and out["reranked"] == 4
    assert out["results"]["email"][0]["_rerank"] == -8.0


def test_rerank_survives_a_failing_service(monkeypatch):
    monkeypatch.setattr(search_rerank, "RERANK_URL", "http://x")

    def boom(query, docs, timeout):
        raise RuntimeError("down")
    monkeypatch.setattr(search_rerank, "_scores", boom)
    raw = {"results": {"email": [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]}}
    assert search_rerank.rerank("q", raw) is raw


# ─── with a database ────────────────────────────────────────────────

@pytest.fixture
def mailbox(fresh_app):
    """Dirk with a mail account; a helper that stores a mail of his."""
    from tests.conftest import login_client
    from backend.database import get_conn
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    with get_conn() as conn:
        acc = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                           "smtp_username, credential_key) VALUES (?, 'd@example.local', 'i', 'u', 's', 'u', 'k') "
                           "RETURNING id", (dirk,)).fetchone()["id"]
        conn.commit()

    def add(uid, subject, from_name, from_email, body_text, body_html=None):
        with get_conn() as conn:
            mid = conn.execute("INSERT INTO email_messages (account_id, uid, subject, from_name, from_email, snippet, "
                               "body_text, body_html, owner_user_id, date_received) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                               "'2026-09-01T10:00:00+00:00') RETURNING id",
                               (acc, uid, subject, from_name, from_email, (body_text or "")[:220], body_text, body_html,
                                dirk)).fetchone()["id"]
            conn.commit()
        return mid
    return {"client": dirk_c, "dirk": dirk, "add": add}


def test_typos_in_names_are_corrected_against_own_senders(mailbox):
    from backend import search_vocab
    from backend.database import get_conn
    search_vocab._available = None
    with get_conn() as conn:
        if not search_vocab.available(conn):
            pytest.skip("pg_trgm not available in this cluster")
    mailbox["add"](1, "Erledigungsbestätigung - Riverty GmbH", "Riverty", "amazon@riverty.com", "Ihre Zahlung ist eingegangen.")
    mailbox["add"](2, "Mit Docusign abschließen", "Nadine Redlich über Docusign", "dse@docusign.net", "Bitte signieren.")
    assert search_vocab.refresh(force=True) > 0
    fixes = search_vocab.correct(["rivertie", "docusing", "zahlung", "xyzzy"], mailbox["dirk"])
    assert fixes == {"rivertie": "riverty", "docusing": "docusign"}     # a word with hits is left alone
    assert search_vocab.apply("rivertie zahlung", fixes) == "riverty zahlung"
    # another person's senders are not suggested
    assert search_vocab.correct(["rivertie"], "00000000-0000-0000-0000-000000000000") == {}


def test_stored_css_becomes_readable_text_and_can_be_undone(mailbox):
    from backend import email_fetcher as ef
    from backend.database import get_conn
    html = "<html><head><style>table{border:0} .ExternalClass{width:100%}</style></head><body><p>Bestellung bestätigt, Dirk!</p></body></html>"
    bad = mailbox["add"](3, "Bestellung #1 bestätigt", "Snuzone", "shop@snuzone.de",
                         "table{border:0} .ExternalClass{width:100%} Bestellung bestätigt, Dirk!", html)
    fine = mailbox["add"](4, "Hallo", "Beate", "b@example.local", "alles gut bei dir?", "<p>alles gut bei dir?</p>")
    assert ef.repair_stored_markup() == 1          # looked at one mail
    assert ef.repair_stored_markup() == 0          # and not again
    with get_conn() as conn:
        row = conn.execute("SELECT body_text, snippet FROM email_messages WHERE id = ?", (bad,)).fetchone()
        assert row["body_text"] == "Bestellung bestätigt, Dirk!" and row["snippet"] == "Bestellung bestätigt, Dirk!"
        assert conn.execute("SELECT body_text FROM email_messages WHERE id = ?", (fine,)).fetchone()["body_text"] == "alles gut bei dir?"
        assert conn.execute("SELECT body_text FROM email_messages WHERE search_tsv @@ to_tsquery('simple', 'bestätigt') AND id = ?",
                            (bad,)).fetchone()                     # the generated tsvector followed
    assert ef.undo_stored_markup_repair() == 1
    with get_conn() as conn:
        assert conn.execute("SELECT body_text FROM email_messages WHERE id = ?", (bad,)).fetchone()["body_text"].startswith("table{")
