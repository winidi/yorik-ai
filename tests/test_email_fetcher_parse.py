"""The fetcher stores a plain-text mail (the folded-subject fix of
2026-09-21 imported `re` only on the HTML path, and every text/plain
mail failed to parse until 09-22)."""

from __future__ import annotations

import mailparser

from tests.conftest import login_client


def _account(conn, owner):
    cols = {r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'email_accounts'").fetchall()}
    vals = {"owner_user_id": owner, "email": "b@example.local", "imap_host": "i", "imap_port": 993, "imap_username": "b", "smtp_host": "s",
            "smtp_port": 465, "smtp_username": "b", "credential_key": "k-b"}
    use = {k: v for k, v in vals.items() if k in cols}
    aid = conn.execute(f"INSERT INTO email_accounts ({', '.join(use)}) VALUES ({', '.join('?' * len(use))}) RETURNING id", tuple(use.values())).fetchone()["id"]
    fid = conn.execute("INSERT INTO email_folders (account_id, name) VALUES (?, 'INBOX') RETURNING id", (aid,)).fetchone()["id"]
    return aid, fid


def test_a_plain_text_mail_with_a_folded_subject_is_stored(fresh_app):
    from backend import email_fetcher as F
    from backend.database import get_conn
    _, uid = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    with get_conn() as conn:
        aid, fid = _account(conn, uid); conn.commit()
        cfg = dict(conn.execute("SELECT * FROM email_accounts WHERE id = ?", (aid,)).fetchone())
    raw = (b"From: Anna <anna@example.org>\r\nTo: b@example.local\r\nSubject: Einladung zum\r\n Sommerfest\r\n"
           b"Message-ID: <plain-1@example.org>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHallo, kommst du?\r\n")
    F._insert_message(cfg, fid, 4711, {b"FLAGS": (), b"RFC822.SIZE": len(raw)}, mailparser.parse_from_bytes(raw))
    with get_conn() as conn:
        row = conn.execute("SELECT subject, snippet FROM email_messages WHERE account_id = ? AND uid = ?", (aid, 4711)).fetchone()
    assert row and row["subject"] == "Einladung zum Sommerfest" and "kommst du" in row["snippet"]
