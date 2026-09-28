"""Threads the Thunderbird way (2026-09-28): Oliver's answer referenced
only Dirk's question, not his own first mail, and stood alone."""

from __future__ import annotations

from tests.conftest import seed_user


def _mail(conn, acc, uid, n, mid, in_reply_to=None, refs=None):
    from backend import email_threads
    thread = email_threads.thread_for(conn, uid, mid, in_reply_to, refs)
    conn.execute("INSERT INTO email_messages (account_id, uid, owner_user_id, message_id, in_reply_to, thread_id, "
                 "from_email, subject, body_text, snippet) VALUES (?, ?, ?, ?, ?, ?, 'x@example.org', 's', '', '')",
                 (acc, n, uid, mid, in_reply_to, thread))
    email_threads.adopt_answers(conn, uid, mid, thread)
    return thread


def _threads(conn, uid):
    return {r["message_id"]: r["thread_id"] for r in conn.execute(
        "SELECT message_id, thread_id FROM email_messages WHERE owner_user_id = ?", (uid,))}


def test_an_answer_joins_the_thread_of_the_mail_it_answers(fresh_app):
    from backend.database import get_conn
    uid = seed_user(name="Dirk", role="admin", email="d@example.com")
    with get_conn() as conn:
        acc = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                           "smtp_username, credential_key) VALUES (?, 'd@example.local', 'i', 'u', 's', 'u', 'k') "
                           "RETURNING id", (uid,)).fetchone()["id"]
        _mail(conn, acc, uid, 1, "oliver-1@x")                                         # Oliver writes
        _mail(conn, acc, uid, 2, "dirk-q@x", in_reply_to="oliver-1@x", refs=["oliver-1@x"])   # Dirk asks
        _mail(conn, acc, uid, 3, "oliver-2@x", in_reply_to="dirk-q@x", refs=["dirk-q@x"])     # only the question
        t = _threads(conn, uid)
        assert t["oliver-1@x"] == t["dirk-q@x"] == t["oliver-2@x"] == "oliver-1@x"


def test_a_parent_that_arrives_late_takes_its_answers_in(fresh_app):
    from backend.database import get_conn
    uid = seed_user(name="Dirk", role="admin", email="d@example.com")
    with get_conn() as conn:
        acc = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                           "smtp_username, credential_key) VALUES (?, 'd@example.local', 'i', 'u', 's', 'u', 'k') "
                           "RETURNING id", (uid,)).fetchone()["id"]
        _mail(conn, acc, uid, 1, "root@x")
        _mail(conn, acc, uid, 3, "answer@x", in_reply_to="question@x", refs=["question@x"])  # before its parent
        _mail(conn, acc, uid, 4, "answer2@x", in_reply_to="answer@x", refs=["question@x", "answer@x"])
        _mail(conn, acc, uid, 2, "question@x", in_reply_to="root@x", refs=["root@x"])       # arrives last
        t = _threads(conn, uid)
        assert t["root@x"] == t["question@x"] == t["answer@x"] == t["answer2@x"] == "root@x"


def test_brackets_and_unknown_parents():
    from backend import email_threads

    class _Conn:
        def execute(self, *a):
            class _R:
                def fetchone(self):
                    return None
            return _R()
    assert email_threads.thread_for(_Conn(), "u", "m@x", "<p@x>", ["<r@x>", "<p@x>"]) == "r@x"
    assert email_threads.thread_for(_Conn(), "u", "m@x", None, None) == "m@x"


def test_thread_endpoint_lists_the_conversation_for_its_owner_only(fresh_app):
    from backend.database import get_conn
    from tests.conftest import login_client
    client, uid = login_client(fresh_app, role="admin", name="Dirk", email="dirk@example.com")
    other_c, other = login_client(fresh_app, role="member", name="Beate", email="beate@example.com")
    with get_conn() as conn:
        acc = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
                           "smtp_username, credential_key) VALUES (?, 'd@example.local', 'i', 'u', 's', 'u', 'k') "
                           "RETURNING id", (uid,)).fetchone()["id"]
        for n, (mid, sent, when) in enumerate((("o1@x", 0, "2026-09-20T00:54:00+00:00"),
                                               ("q@x", 1, "2026-09-21T14:22:00+00:00"),
                                               ("q@x", 1, "2026-09-21T14:22:00+00:00"),      # same mail, 2nd folder
                                               ("o2@x", 0, "2026-09-21T22:00:00+00:00"))):
            conn.execute("INSERT INTO email_messages (account_id, uid, owner_user_id, message_id, thread_id, from_email, "
                         "subject, body_text, snippet, date_received, is_sent) VALUES (?, ?, ?, ?, 'o1@x', "
                         "'o@example.org', 'GoHighLevel', '', ?, ?, ?)", (acc, n + 1, uid, mid, f"text {n}", when, sent))
        conn.commit()
    got = client.get("/api/email/thread", params={"thread_id": "o1@x"}).json()["messages"]
    assert [(m["snippet"], m["is_sent"]) for m in got] == [("text 0", False), ("text 1", True), ("text 3", False)]
    assert other_c.get("/api/email/thread", params={"thread_id": "o1@x"}).json()["messages"] == []
