"""Diagnostics, the pieces that must hold before anything is sent:
pseudonyms are stable and one-way, the registry lets only listed
fields through, the scrubber replaces what it must and the last look
(self_check) catches what slipped. Plan 2026-10-06."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def house(fresh_app):
    """Dirk (admin) with a contact, a mail account, a chat and a sender."""
    from tests.conftest import seed_user
    from backend.database import get_conn
    dirk = seed_user(name="Dirk Winiecki", email="dirk@example.local", role="platform_admin",
                     first_name="Dirk", last_name="Winiecki", phone="+49 171 2345678")
    with get_conn() as conn:
        cid = conn.execute("INSERT INTO contacts (display_name, first_name, last_name, status, kind, iban) "
                           "VALUES ('Beate Mayer', 'Beate', 'Mayer', 'active', 'person', 'DE89 3704 0044 0532 0130 00') "
                           "RETURNING id").fetchone()["id"]
        conn.execute("INSERT INTO contact_channels (contact_id, kind, value) VALUES (?, 'email', 'beatemayer1@gmx.net')", (cid,))
        conn.execute("INSERT INTO contact_channels (contact_id, kind, value) VALUES (?, 'phone', '+491601234567')", (cid,))
        conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, smtp_username, "
                     "credential_key) VALUES (?, 'winidi89@gmail.com', 'i', 'u', 's', 'u', 'k')", (dirk,))
        conn.execute("INSERT INTO wa_chats (jid, name, is_group, owner_user_id) VALUES ('4915112345678@s.whatsapp.net', 'Jan Tronycs', 0, ?)", (dirk,))
        conn.execute("INSERT INTO wa_chats (jid, name, is_group, owner_user_id) VALUES ('1203@g.us', 'Ideenschmiede Peine', 1, ?)", (dirk,))
        conn.commit()
    return dirk


def test_tokens_are_stable_per_kind_and_forgotten_on_reset(house):
    from backend.diagnostics import pseudonyms as P, reset_identity
    a = P.token_for("person", "Beate Mayer")
    assert a == "person_1" and P.token_for("person", "beate  MAYER") == a
    assert P.token_for("person", "Jan Tronycs") == "person_2"
    assert P.token_for("email", "Beate@GMX.net") == "email_1" and P.token_for("email", "beate@gmx.net") == "email_1"
    assert P.token_for("phone", "+49 171 2345678") == P.token_for("phone", "0171 2345678") == "phone_1"
    assert P.token_for("iban", "DE89 3704 0044 0532 0130 00") == P.token_for("iban", "DE89370400440532013000")
    assert P.token_for("person", "") is None
    with pytest.raises(ValueError):
        P.token_for("planet", "Mars")
    out = reset_identity()
    assert out["pseudonyms_forgotten"] >= 5
    assert P.token_for("person", "Jan Tronycs") == "person_1"       # a new secret, a new count


def test_who_is_answers_from_the_dictionary_only(house):
    from backend.diagnostics import pseudonyms as P
    from backend.database import get_conn
    tok = P.token_for("person", "Beate Mayer")
    hits = P.who_is(tok, house)
    assert {h["value"] for h in hits} == {"Beate Mayer"}
    assert P.who_is("person_99", house) == [] and P.who_is("drop table", house) == []
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM diag_pseudonyms WHERE token = ?", (tok,)).fetchone()
    assert "Beate" not in json.dumps(dict(row), default=str)        # nothing in clear


def test_dictionary_holds_the_house_and_the_senders(house):
    from backend.diagnostics import pseudonyms as P
    from backend.database import get_conn
    with get_conn() as conn:
        acc = conn.execute("SELECT id FROM email_accounts WHERE owner_user_id = ?", (house,)).fetchone()["id"]
        conn.execute("INSERT INTO email_messages (account_id, uid, subject, from_name, from_email, owner_user_id, date_received) "
                     "VALUES (?, 1, 'Rechnung', 'Riverty GmbH', 'amazon@riverty.com', ?, '2026-10-01T10:00:00+00:00')", (acc, house))
        conn.commit()
    entries = {(k, v) for k, v, _ in P.build_dictionary(house)}
    for kind, value in [("person", "Beate Mayer"), ("person", "Beate"), ("person", "Mayer"), ("email", "beatemayer1@gmx.net"),
                        ("phone", "+491601234567"), ("mailbox", "winidi89@gmail.com"), ("chat", "4915112345678@s.whatsapp.net"),
                        ("group", "Ideenschmiede Peine"), ("person", "Jan Tronycs"), ("email", "amazon@riverty.com"),
                        ("person", "Riverty GmbH"), ("person", "Dirk"), ("iban", "DE89 3704 0044 0532 0130 00")]:
        assert (kind, value) in entries, (kind, value)
    assert ("person", "GmbH") not in entries and ("person", "Dr") not in entries
    assert dict(((k, v), c) for k, v, c in P.build_dictionary(house))[("person", "Beate")] == "Beate Mayer"


def test_scrub_replaces_known_people_inflections_and_unknown_addresses(house):
    from backend.diagnostics import pseudonyms as P, scrub as S
    dictionary = P.build_dictionary(house)
    text = ("Hat Beate Mayer mir Beates Termin geschickt? Jan schrieb an winidi89@gmail.com und an fremd@example.org, "
            "Tel 0160 1234567, IBAN DE89 3704 0044 0532 0130 00, am 2.7. um 14 Uhr, 54,65 € https://x.y/z?t=1 "
            "Sendung 00340434796242133711, key sk-or-v1-abcdefghijklmnopqrstuvwxyz")
    out, counts = S.scrub_text(text, dictionary)
    for raw in ("Beate", "Mayer", "Jan", "winidi89", "fremd@example.org", "1234567", "DE89", "2.7.", "54,65", "x.y/z",
                "0034043", "sk-or-v1"):
        assert raw not in out, (raw, out)
    assert out.startswith("Hat person_1 mir person_1 Termin")          # Beate Mayer and Beates: one token
    assert "email_" in out and "[date]" in out and "[amount]" in out and "[url]" in out
    assert "[secret]" in out and "[number]" in out
    assert counts["person"] >= 3 and counts["email"] >= 1 and counts["mailbox"] >= 1 and counts["phone"] >= 1 and counts["iban"] >= 1
    # the same unknown address gets the same token next time
    again, _ = S.scrub_text("fremd@example.org", dictionary)
    assert again in out


def test_free_text_is_dropped_without_a_local_model(house, monkeypatch):
    from backend.diagnostics import pseudonyms as P, scrub as S
    import httpx
    calls = []
    monkeypatch.setattr(httpx.Client, "post", lambda self, *a, **k: calls.append(a) or (_ for _ in ()).throw(RuntimeError("no net")))
    monkeypatch.setenv("HOMEOS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    assert S.llm_is_local() is False
    out, level, dropped = S.scrub_free_text("Frau Lehmann aus Schwabing", P.build_dictionary(house), {})
    assert out == "" and dropped is True and level == "dictionary_regex" and calls == []
    for url, local in (("http://127.0.0.1:8080/v1", True), ("http://ninfer:8080/v1", True), ("http://192.168.1.4:8080/v1", True),
                       ("https://api.openai.com/v1", False), ("http://llm.example.com/v1", False)):
        assert S.llm_is_local(url) is local, url


def test_local_model_names_become_tokens(house, monkeypatch):
    from backend.diagnostics import pseudonyms as P, scrub as S
    import httpx
    monkeypatch.setenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1")

    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": '{"found": [{"text": "Frau Lehmann", "kind": "person"}, {"text": "Schwabing", "kind": "place"}]}'}}]}
    monkeypatch.setattr(httpx.Client, "post", lambda self, *a, **k: _R())
    out, level, dropped = S.scrub_free_text("Hat Frau Lehmann aus Schwabing geschrieben?", P.build_dictionary(house), {})
    assert dropped is False and level == "dictionary_regex_llm"
    assert "Lehmann" not in out and "Schwabing" not in out and "person_" in out and "[place]" in out


def test_errors_become_class_frames_and_template():
    from backend.diagnostics import scrub as S
    tb = ('Traceback (most recent call last):\n  File "/home/isee/yorikai/yorik-ai/backend/skills/registry.py", line 470, in invoke\n'
          '    out = fn(**args)\n  File "/home/isee/yorikai/yorik-ai/venv/lib/python3.12/site-packages/psycopg/cursor.py", line 117, in execute\n'
          '    raise ex\npsycopg.errors.StringDataRightTruncation: value "Beate Mayer <beate@gmx.net>" too long for type character varying(20)\n')
    e = S.scrub_error('psycopg.errors.StringDataRightTruncation: value "Beate Mayer <beate@gmx.net>" too long', tb)
    assert e["class"] == "psycopg.errors.StringDataRightTruncation"
    assert e["frames"] == "skills/registry.py:invoke:470"            # site-packages and /home are gone
    assert "Beate" not in e["template"] and "gmx" not in e["template"] and "'…'" in e["template"]
    e2 = S.scrub_error("no contact named 'Beate Mayer' (id 42) at https://x.y/a?b=1")
    assert e2["template"] == "no contact named '…' (id #) at [url]"
    assert S.route_template("/api/contacts/42/channels?x=1") == "/api/contacts/{id}/channels"
    assert S.route_template("/api/conversations/76439d8d-f2fa-4bae-9f8b-f0be620186ca") == "/api/conversations/{id}"


def test_results_and_args_are_shapes_and_tokens(house):
    from backend.diagnostics import scrub as S, pseudonyms as P
    shape = S.result_shape(json.dumps({"query": "x", "total": 7, "results": {"email": [{"title": "Rechnung Beate"}] * 3, "bank": []}}))
    assert shape["total"] == 7 and shape["sources"] == {"email": 3, "bank": 0} and "Beate" not in json.dumps(shape)
    assert S.result_shape("Error: no contact named 'Beate'")["error_class"] == "Error"
    assert S.result_shape("")["empty"] is True
    counts = {}
    args = S.scrub_args({"to": "beatemayer1@gmx.net", "subject": "Hose", "body": "Hallo Beate", "source": "email",
                         "contact": "Beate Mayer", "tone": "friendly", "kind": "Beate"}, P.build_dictionary(house), counts)
    assert set(args) == {"to", "source", "contact", "tone"}
    assert args["to"].startswith("email_") and args["contact"].startswith("person_") and args["source"] == "email"
    assert "Hose" not in json.dumps(args) and "Hallo" not in json.dumps(args)


def test_registry_keeps_only_listed_fields_and_checks_types():
    from backend.diagnostics import registry as R
    payload = {"schema": 1, "kind": "error", "report_id": "0b45d20c-3b93-4f04-b970-83db1fb047a9",
               "env": {"version": "0.3", "platform": "linux-x64", "hostname": "isee-box", "llm_kind": "local"},
               "question": {"text": "wo ist person_1", "intent": "search", "words": "4-9", "raw": "wo ist Beate"},
               "trace": [{"name": "universal_search", "arg_keys": "query", "args": {"to": "email_2", "body": "Hallo"},
                          "result": {"len": "100-999", "total": 7, "sources": {"email": 3}, "text": "Rechnung Beate"}}],
               "facts": [{"token": "person_1", "kind": "person", "exists": True, "last_seen": "2026-10", "name": "Beate"}],
               "counts": {"users": "3-4", "chat_turns": 999}}
    clean, dropped = R.serialise(payload, max_tier=3)
    text = json.dumps(clean)
    assert "hostname" not in text and "Beate" not in text and "Hallo" not in text and "raw" not in text
    assert clean["trace"][0]["args"] == {"to": "email_2"} and clean["trace"][0]["result"]["total"] == 7
    assert clean["facts"][0]["last_seen"] == "2026-10" and dropped >= 6
    # tier 1 drops everything above it
    tier1, _ = R.serialise(payload, max_tier=1)
    assert "question" not in tier1 and "trace" not in tier1 and tier1["env"]["platform"] == "linux-x64"
    assert "chat_turns" not in tier1["counts"]            # 999 is not a bucket
    # every field is documented
    for f in R.FIELDS:
        assert f.purpose and f.tier in (1, 2, 3) and f.type in (
            "enum", "bucket", "smallint", "bool", "month", "day", "token", "token_or_enum", "text", "id", "version")


def test_self_check_finds_what_slipped(house):
    from backend.diagnostics import pseudonyms as P, scrub as S
    d = P.build_dictionary(house)
    assert S.self_check(json.dumps({"q": "wo ist person_1 [date]"}), d) == []
    assert "person" in S.self_check(json.dumps({"q": "wo ist Beate Mayer"}), d)
    assert "email" in S.self_check('{"x": "a@b.de"}', d) and "secret" in S.self_check('{"k": "sk-or-v1-abcdefghijklmnopqrstuvwxyz"}', d)
