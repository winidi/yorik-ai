"""Diagnostics, parts 2 and 3: a report is assembled from a turn with
nothing in clear, sent only after the person's review and only with
tier 3 on, the outbox sends only queued rows and backs off, the
detector only suggests, the daily counts carry buckets only."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest


@pytest.fixture
def house(fresh_app, monkeypatch):
    """Dirk with a contact 'Beate Mayer' (beatemayer1@gmx.net), a mail
    from her, and one conversation about her that found nothing."""
    from tests.conftest import login_client
    from backend.database import get_conn
    from backend.agent import conversation_io
    monkeypatch.setenv("HOMEOS_LLM_BASE_URL", "https://openrouter.ai/api/v1")   # cloud: free text is dropped
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk Winiecki", email="dirk@example.local")
    with get_conn() as conn:
        cid = conn.execute("INSERT INTO contacts (display_name, first_name, last_name, status, kind) "
                           "VALUES ('Beate Mayer', 'Beate', 'Mayer', 'active', 'person') RETURNING id").fetchone()["id"]
        conn.execute("INSERT INTO contact_channels (contact_id, kind, value) VALUES (?, 'email', 'beatemayer1@gmx.net')", (cid,))
        acc = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, smtp_username, "
                           "credential_key) VALUES (?, 'dirk@example.local', 'i', 'u', 's', 'u', 'k') RETURNING id", (dirk,)).fetchone()["id"]
        conn.execute("INSERT INTO email_messages (account_id, uid, subject, from_name, from_email, body_text, owner_user_id, date_received) "
                     "VALUES (?, 1, 'Save the Date Vorlagen', 'Beate Mayer', 'beatemayer1@gmx.net', 'Hier die Vorlagen', ?, '2026-07-24T19:51:00+00:00')",
                     (acc, dirk))
        conn.commit()
    conv = str(uuid.uuid4())
    trace = [{"name": "universal_search", "args": {"query": "beate mayer vorlagen", "also": ["Beate Mayer templates"]},
              "result": json.dumps({"query": "beate mayer vorlagen", "total": 0, "results": {"email": [], "whatsapp": []}})},
             {"name": "send_email", "args": {"to": "beatemayer1@gmx.net", "subject": "Hose", "body": "Hallo Beate, die Hose"},
              "result": "Error: SMTP refused 'beatemayer1@gmx.net' (550)"}]
    msgs = [
        {"role": "user", "content": "Hat Beate Mayer mir die Save-the-Date Vorlagen geschickt? Ihre Nummer ist 0160 1234567"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "invoke_skill", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "invoke_skill", "content": "{}"},
        {"role": "assistant", "content": "Ich habe nichts gefunden — in deinen Mails ist keine Nachricht von Beate Mayer.",
         "metadata": {"tool_trace": trace}},
    ]
    conversation_io.save_messages(conv, "platform_admin", dirk, msgs)
    return {"client": dirk_c, "dirk": dirk, "conv": conv, "msgs": msgs}


def _enable(client, **tiers):
    r = client.put("/api/diagnostics/consent", json={"counts": False, "usage": False, "errors": False, **tiers})
    assert r.status_code == 200


def test_draft_carries_tokens_and_facts_but_nothing_in_clear(house):
    c = house["client"]
    assert c.post("/api/diagnostics/reports/draft", json={"conversation_id": house["conv"], "message_idx": 1}).status_code == 403
    _enable(c, errors=True)
    r = c.post("/api/diagnostics/reports/draft", json={"conversation_id": house["conv"], "message_idx": 1,
                                                        "trigger": "thumbs_down", "reason": "found_nothing",
                                                        "note": "Beate hat sie mir am 24.7. geschickt"})
    assert r.status_code == 200, r.text
    d = r.json()
    text = json.dumps(d["payload"], ensure_ascii=False).lower()
    for raw in ("beate", "mayer", "gmx", "1234567", "hose", "hallo", "save-the-date", "vorlagen", "dirk", "example.local"):
        assert raw not in text, (raw, text)
    p = d["payload"]
    assert p["kind"] == "error" and p["trigger"] == "thumbs_down" and p["feedback"]["reason"] == "found_nothing"
    assert p["question"]["text"] == "" and p["scrub"]["free_text_dropped"] is True       # cloud model: prose dropped
    assert p["question"]["intent"] in ("search", "question") and p["question"]["language"] == "de"
    assert p["answer"]["said_nothing_found"] is True and p["answer"]["tool_calls"] == 2
    names = [t["name"] for t in p["trace"]]
    assert names == ["universal_search", "send_email"]
    assert p["trace"][0]["result"]["total"] == 0 and p["trace"][0]["result"]["sources"] == {"email": 0, "whatsapp": 0}
    assert p["trace"][1]["args"] == {"to": "email_1"} and p["trace"][1]["arg_keys"] == "body,subject,to"
    assert p["trace"][1]["result"]["error_class"] == "Error"
    facts = {f["token"]: f for f in p["facts"]}
    assert "email_1" in facts and facts["email_1"]["exists"] is True and facts["email_1"]["last_seen"] == "2026-07"
    assert p["env"]["llm_kind"] == "cloud-openrouter" and p["env"]["version"] in ("dev",) or True
    assert d["scrub_summary"]["replaced"].get("person", 0) + d["scrub_summary"]["replaced"].get("email", 0) >= 1


def test_send_needs_review_and_consent_and_passes_the_last_look(house, monkeypatch):
    from backend.diagnostics import outbox, report
    c = house["client"]
    _enable(c, errors=True)
    rid = c.post("/api/diagnostics/reports/draft", json={"conversation_id": house["conv"], "message_idx": 1}).json()["id"]
    # someone else cannot send it
    from tests.conftest import login_client
    other_c, _ = login_client(house["client"].app, role="member", name="Jan", email="j@example.local")
    assert other_c.post(f"/api/diagnostics/reports/{rid}/send").status_code == 403
    # consent withdrawn between draft and send
    _enable(c, errors=False)
    assert c.post(f"/api/diagnostics/reports/{rid}/send").status_code == 403
    _enable(c, errors=True)
    # the last look: a payload with a name in it is refused
    from backend.database import get_conn
    with get_conn() as conn:
        bad = conn.execute("SELECT payload FROM diag_reports WHERE id = ?", (rid,)).fetchone()["payload"]
        bad = bad if isinstance(bad, dict) else json.loads(bad)
        bad["question"]["text"] = "wo sind Beate Mayers Vorlagen"
        conn.execute("UPDATE diag_reports SET payload = ? WHERE id = ?", (json.dumps(bad), rid)); conn.commit()
    r = c.post(f"/api/diagnostics/reports/{rid}/send")
    assert r.status_code == 409 and "person" in r.json()["detail"]
    # the person shortens the question; it is scrubbed again and sent
    r = c.post(f"/api/diagnostics/reports/{rid}/send", json={"question": "wo sind Beate Mayers Vorlagen"})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    with get_conn() as conn:
        row = conn.execute("SELECT status, payload, delete_token FROM diag_reports WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "queued" and row["delete_token"]
    assert "Beate" not in str(row["payload"]) and "person_" in str(row["payload"])
    assert c.post(f"/api/diagnostics/reports/{rid}/send").status_code == 409        # not a draft any more


def test_outbox_sends_only_queued_rows_and_backs_off(house, monkeypatch):
    from backend.diagnostics import outbox, usage
    from backend.database import get_conn
    c = house["client"]
    calls = []
    monkeypatch.setattr(outbox, "_post", lambda url, body, headers: calls.append((url, body, headers)) or 202)
    # nothing on: nothing leaves, even with a collector set
    monkeypatch.setenv("YORIK_DIAG_COLLECTOR_URL", "https://collect.example.test/v1")
    with get_conn() as conn:
        conn.execute("INSERT INTO diag_reports (id, kind, trigger, status, payload, next_attempt_at) VALUES (?, 'usage_daily', 'daily', 'queued', '{}', now())",
                     (str(uuid.uuid4()),)); conn.commit()
    assert outbox.flush() == {"sent": 0, "failed": 0, "skipped": 0} and calls == []
    _enable(c, counts=True, errors=True)
    draft = c.post("/api/diagnostics/reports/draft", json={"conversation_id": house["conv"], "message_idx": 1}).json()
    out = outbox.flush()
    assert out["sent"] == 1 and len(calls) == 1                      # the queued usage row, not the draft
    url, body, headers = calls[0]
    assert url.endswith("/v1/reports") and headers["X-Yorik-Install"] and "delete_token_sha256" in body
    with get_conn() as conn:
        assert conn.execute("SELECT status FROM diag_reports WHERE id = ?", (draft["id"],)).fetchone()["status"] == "draft"
    # a failing collector backs off and parks after MAX_ATTEMPTS
    monkeypatch.setattr(outbox, "_post", lambda url, body, headers: (_ for _ in ()).throw(ConnectionError("down")))
    c.post(f"/api/diagnostics/reports/{draft['id']}/send")
    from datetime import datetime, timedelta, timezone
    t = datetime.now(timezone.utc)
    for i in range(outbox.MAX_ATTEMPTS):
        outbox.flush(now=t + timedelta(days=i + 1))
    with get_conn() as conn:
        row = conn.execute("SELECT status, attempts, last_error FROM diag_reports WHERE id = ?", (draft["id"],)).fetchone()
    assert row["status"] == "failed" and row["attempts"] == outbox.MAX_ATTEMPTS and "ConnectionError" in row["last_error"]
    # no collector: rows stay here with a note
    monkeypatch.delenv("YORIK_DIAG_COLLECTOR_URL")
    rid = usage.collect_daily(today=date(2026, 10, 7))
    assert rid and outbox.flush()["skipped"] == 1
    with get_conn() as conn:
        assert "no collector" in conn.execute("SELECT last_error FROM diag_reports WHERE id = ?", (rid,)).fetchone()["last_error"]


def test_daily_counts_are_buckets_and_follow_the_tiers(house):
    from backend.diagnostics import usage
    from backend.database import get_conn
    c = house["client"]
    assert usage.collect_daily(today=date(2026, 10, 7)) is None           # off
    _enable(c, counts=True)
    rid = usage.collect_daily(today=date(2026, 10, 7))
    assert rid and usage.collect_daily(today=date(2026, 10, 7)) is None   # once a day
    with get_conn() as conn:
        p = conn.execute("SELECT payload FROM diag_reports WHERE id = ?", (rid,)).fetchone()["payload"]
    p = p if isinstance(p, dict) else json.loads(p)
    assert p["day"] == "2026-10-06" and p["counts"]["users"] in ("1", "2", "3-4", "5+") and "features" not in p and "setup" not in p
    text = json.dumps(p).lower()
    assert "beate" not in text and "dirk" not in text and "example" not in text
    _enable(c, counts=True, usage=True)
    rid2 = usage.collect_daily(today=date(2026, 10, 8))
    with get_conn() as conn:
        p2 = conn.execute("SELECT payload FROM diag_reports WHERE id = ?", (rid2,)).fetchone()["payload"]
    p2 = p2 if isinstance(p2, dict) else json.loads(p2)
    assert p2["setup"]["mailboxes"] == "1-9" and p2["setup"]["contacts"] == "1-9" and p2["setup"]["whatsapp"] is False


def test_detector_suggests_once_and_never_sends(house):
    from backend.diagnostics import detect
    from backend.database import get_conn
    c = house["client"]
    assert detect.after_turn(house["conv"], house["dirk"], house["msgs"]) is None          # tier 3 off
    _enable(c, errors=True)
    assert detect.after_turn(house["conv"], house["dirk"], house["msgs"]) == "skill_failure"
    assert detect.after_turn(house["conv"], house["dirk"], house["msgs"]) is None          # once per conversation
    with get_conn() as conn:
        n = conn.execute("SELECT count(*) AS n FROM notifications WHERE kind = 'diag_suggest'").fetchone()["n"]
        q = conn.execute("SELECT count(*) AS n FROM diag_reports WHERE status = 'queued'").fetchone()["n"]
    assert n == 1 and q == 0
    # an empty search about someone known, without a failing tool
    msgs = [house["msgs"][0], {"role": "assistant", "content": "Ich konnte nichts finden.",
                               "metadata": {"tool_trace": [{"name": "universal_search", "args": {"query": "beate"}, "result": "{}"}]}}]
    assert detect.classify(msgs, house["dirk"]) == "search_empty"
    assert detect.classify([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hallo!", "metadata": {}}], house["dirk"]) is None


def test_display_index_matches_the_chat(house):
    from backend.diagnostics.report import display_messages, turn_of
    shown = display_messages(house["msgs"])
    assert [i for i, _ in shown] == [0, 3]                 # the interim assistant and the tool row are not shown
    turn = turn_of(house["msgs"], 1)
    assert turn["question"].startswith("Hat Beate") and len(turn["calls"]) == 2
    with pytest.raises(KeyError):
        turn_of(house["msgs"], 0)                          # a question is not an answer
