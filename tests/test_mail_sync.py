"""Mail sync that loses nothing — against an in-memory IMAP server.

The cases come from Beate's GMX account (2026-09-23): the first import
took "the last 200 UID numbers", which after years of deleting held 17
mails; a parse bug on 09-21 skipped 54 mails for good because the
watermark moved past them; a copy of a mail in two folders hopped
between them; and a delete could end in erasing a mail for good."""

from __future__ import annotations

import pytest

from tests.conftest import login_client
from tests.fake_imap import FakeServer


# ─── setup ──────────────────────────────────────────────────────────

@pytest.fixture
def mailbox(fresh_app, monkeypatch):
    """(server, cfg, helpers) for Beate with one account on a fake GMX."""
    from backend import credential_store, email_fetcher as F
    from backend.database import get_conn
    client, uid = login_client(fresh_app, role="member", name="Beate", email="beate@example.test")
    server = FakeServer()
    monkeypatch.setattr(credential_store, "get", lambda key: {"imap_password": "pw", "smtp_password": "pw"})
    monkeypatch.setattr(F, "IMAPClient", lambda *a, **k: server.client())
    monkeypatch.setattr(F, "POLL_FALLBACK_S", 0)
    from backend import email_actions
    monkeypatch.setattr(email_actions, "IMAPClient", lambda *a, **k: server.client())
    drafts: list = []
    from backend import email_autodraft
    monkeypatch.setattr(email_autodraft, "schedule_for_message", lambda *a, **k: drafts.append(a))

    async def _no_reload(_aid):
        return None
    monkeypatch.setattr(F, "reload_account", _no_reload)

    with get_conn() as conn:
        aid = conn.execute(
            "INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_port, imap_username, "
            "smtp_host, smtp_port, smtp_username, credential_key) "
            "VALUES (?, 'beate@example.test', 'imap.example.test', 993, 'beate', 'smtp.example.test', 465, "
            "'beate', 'k') RETURNING id", (uid,)).fetchone()["id"]
        conn.commit()

    class H:
        pass
    h = H()
    h.server, h.aid, h.uid, h.http, h.drafts = server, aid, uid, client, drafts

    def cfg():
        return F._load_account_config(aid)
    h.cfg = cfg

    def rows(folder=None):
        with get_conn() as conn:
            sql = ("SELECT m.id, m.uid, m.subject, m.message_id, m.is_unread, f.name AS folder "
                   "FROM email_messages m JOIN email_folders f ON f.id = m.folder_id WHERE m.account_id=?")
            args = [aid]
            if folder:
                sql += " AND f.name=?"
                args.append(folder)
            return [dict(r) for r in conn.execute(sql + " ORDER BY m.uid", args).fetchall()]
    h.rows = rows

    def sync_pass(budget=F.PASS_BUDGET):
        c = server.client()
        F._enumerate_folders(c, aid)
        return F.reconcile_account(c, cfg(), budget=budget)
    h.sync_pass = sync_pass

    def run_once():
        F._run_once(cfg())
    h.run_once = run_once

    def folder_id(name):
        with get_conn() as conn:
            return conn.execute("SELECT id FROM email_folders WHERE account_id=? AND name=?", (aid, name)).fetchone()["id"]
    h.folder_id = folder_id

    def scope(value):
        with get_conn() as conn:
            conn.execute("UPDATE email_accounts SET import_scope=? WHERE id=?", (value, aid))
            conn.commit()
    h.scope = scope
    return h


def sparse_inbox(server, keep: int, numbers: int):
    """An inbox after years of use: `numbers` UIDs handed out, `keep` mails
    left, spread over the whole range."""
    inbox = server.folders["INBOX"]
    step = numbers // keep
    for n in range(keep):
        server.deliver("INBOX", f"Mail {n}", days_old=(keep - n))
        uid = max(inbox.msgs)
        inbox.msgs[(n + 1) * step] = inbox.msgs.pop(uid)
    inbox.next_uid = numbers + 1


# ─── importing ──────────────────────────────────────────────────────

def test_the_first_import_brings_the_latest_mails_not_the_latest_numbers(mailbox):
    sparse_inbox(mailbox.server, keep=60, numbers=6000)   # 100 numbers apart
    mailbox.run_once()
    assert len(mailbox.rows("INBOX")) == 60              # the old way found 2 of them


def test_import_scope_all_brings_everything_in_passes(mailbox):
    for n in range(700):
        mailbox.server.deliver("INBOX", f"Mail {n}", days_old=700 - n)
    mailbox.scope("all")
    first = mailbox.sync_pass(budget=300)
    assert first["phase"] == "importing" and first["pending"] == 400
    mailbox.sync_pass(budget=300)
    last = mailbox.sync_pass(budget=300)
    assert last["phase"] == "in_sync" and len(mailbox.rows("INBOX")) == 700


def test_import_scope_in_days(mailbox):
    for age in (1, 10, 40, 400):
        mailbox.server.deliver("INBOX", f"{age} Tage alt", days_old=age)
    mailbox.scope("days:30")
    mailbox.sync_pass()
    assert sorted(r["subject"] for r in mailbox.rows("INBOX")) == ["1 Tage alt", "10 Tage alt"]


# ─── nothing is skipped ─────────────────────────────────────────────

def test_a_mail_that_fails_is_retried_not_skipped(mailbox, monkeypatch):
    from backend import email_fetcher as F
    for n in range(5):
        mailbox.server.deliver("INBOX", f"Mail {n}")
    real = F._insert_message
    broken = {"on": True}

    def flaky(cfg, folder_id, uid, data, parsed, quiet=False):
        if broken["on"] and parsed.subject == "Mail 2":
            raise RuntimeError("parser hiccup")
        return real(cfg, folder_id, uid, data, parsed, quiet=quiet)
    monkeypatch.setattr(F, "_insert_message", flaky)

    mailbox.run_once()
    assert [r["subject"] for r in mailbox.rows("INBOX")] == ["Mail 0", "Mail 1", "Mail 3", "Mail 4"]
    mailbox.server.deliver("INBOX", "Mail 5")              # later mail is not held back
    broken["on"] = False
    mailbox.sync_pass()
    assert [r["subject"] for r in mailbox.rows("INBOX")] == [f"Mail {n}" for n in range(6)]  # Mail 2 on the retry
    from backend.database import get_conn
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM email_fetch_failures").fetchone()["n"] == 0


def test_a_mail_that_keeps_failing_is_parked_shown_and_repairable(mailbox, monkeypatch):
    from backend import email_fetcher as F
    mailbox.server.deliver("INBOX", "Kaputt")
    real = F._insert_message
    monkeypatch.setattr(F, "_insert_message",
                        lambda cfg, fid, uid, data, parsed, quiet=False: (_ for _ in ()).throw(ValueError("bad charset")))
    mailbox.run_once()
    for _ in range(F.MAX_ATTEMPTS):
        mailbox.sync_pass()
    accounts = mailbox.http.get("/api/email/accounts").json()
    assert accounts[0]["failed_mails"] == 1
    failures = mailbox.http.get(f"/api/email/accounts/{mailbox.aid}/failures").json()["failures"]
    assert failures[0]["parked"] == 1 and "bad charset" in failures[0]["last_error"]

    monkeypatch.setattr(F, "_insert_message", real)
    assert mailbox.http.post(f"/api/email/accounts/{mailbox.aid}/repair").status_code == 200
    mailbox.sync_pass()
    assert [r["subject"] for r in mailbox.rows("INBOX")] == ["Kaputt"]
    assert mailbox.http.get("/api/email/accounts").json()[0]["failed_mails"] == 0


def test_a_database_error_is_not_swallowed(mailbox, monkeypatch):
    """_insert_message used to log a failed INSERT and carry on; the
    fetcher counted the mail as stored and moved past it."""
    from contextlib import contextmanager
    from backend import email_fetcher as F
    real_get_conn = F.get_conn

    class Refusing:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO email_messages") and "Verweigert" in [str(p) for p in params]:
                raise RuntimeError("disk full")
            return self._c.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._c, name)

    @contextmanager
    def refusing_get_conn(*a, **k):
        with real_get_conn(*a, **k) as conn:
            yield Refusing(conn)
    monkeypatch.setattr(F, "get_conn", refusing_get_conn)

    mailbox.server.deliver("INBOX", "Verweigert")
    mailbox.server.deliver("INBOX", "Normal")
    mailbox.run_once()
    from backend.database import get_conn
    with get_conn() as conn:
        failed = conn.execute("SELECT uid, last_error FROM email_fetch_failures").fetchall()
    assert [r["subject"] for r in mailbox.rows("INBOX")] == ["Normal"]
    assert len(failed) == 1 and "disk full" in failed[0]["last_error"]
    monkeypatch.setattr(F, "get_conn", real_get_conn)
    mailbox.sync_pass()
    assert {r["subject"] for r in mailbox.rows("INBOX")} == {"Normal", "Verweigert"}


# ─── Beate's gaps, filled quietly ───────────────────────────────────

def test_the_sync_pass_fills_gaps_quietly(mailbox):
    for n in range(20):
        mailbox.server.deliver("INBOX", f"Ihre Rechnung {n}", days_old=30 - n, sender="rechnung@stadtwerke.example")
    mailbox.run_once()
    from backend.database import get_conn
    with get_conn() as conn:                               # the 09-21 bug: five never stored
        conn.execute("DELETE FROM email_messages WHERE account_id=? AND uid IN (3, 7, 11, 12, 18)", (mailbox.aid,))
        conn.execute("DELETE FROM notifications")
        conn.commit()
    mailbox.drafts.clear()
    state = mailbox.sync_pass()
    assert state["fetched"] == 5 and len(mailbox.rows("INBOX")) == 20
    with get_conn() as conn:
        bell = conn.execute("SELECT COUNT(*) AS n FROM notifications").fetchone()["n"]
    assert bell == 0 and mailbox.drafts == []              # old mail is not news


def test_new_mail_found_by_the_pass_still_notifies(mailbox):
    mailbox.run_once()
    mailbox.drafts.clear()
    mailbox.server.deliver("INBOX", "Kommst du heute?", days_old=0.1)
    mailbox.sync_pass()
    assert len(mailbox.drafts) == 1


# ─── Yorik follows the server ───────────────────────────────────────

def test_a_mail_deleted_elsewhere_leaves_yorik(mailbox):
    for n in range(3):
        mailbox.server.deliver("INBOX", f"Mail {n}")
    mailbox.run_once()
    mailbox.server.folders["INBOX"].msgs.pop(2)            # deleted on the phone
    mailbox.sync_pass()
    assert [r["subject"] for r in mailbox.rows("INBOX")] == ["Mail 0", "Mail 2"]


def test_a_mail_marked_deleted_elsewhere_counts_as_deleted(mailbox):
    for n in range(2):
        mailbox.server.deliver("INBOX", f"Mail {n}")
    mailbox.run_once()
    mailbox.server.folders["INBOX"].msgs[1]["flags"].add(b"\\Deleted")
    mailbox.sync_pass()
    assert [r["subject"] for r in mailbox.rows("INBOX")] == ["Mail 1"]


def test_nothing_is_dropped_when_the_server_answers_nothing(mailbox):
    for n in range(3):
        mailbox.server.deliver("INBOX", f"Mail {n}")
    mailbox.run_once()
    for m in mailbox.server.folders["INBOX"].msgs.values():   # SEARCH UNDELETED comes back empty
        m["flags"].add(b"\\Deleted")
    mailbox.sync_pass()
    assert len(mailbox.rows("INBOX")) == 3


def test_read_and_flagged_come_from_the_server(mailbox):
    mailbox.server.deliver("INBOX", "Gelesen auf dem Handy")
    mailbox.run_once()
    assert mailbox.rows("INBOX")[0]["is_unread"] == 1
    mailbox.server.folders["INBOX"].msgs[1]["flags"].add(b"\\Seen")
    mailbox.sync_pass()
    assert mailbox.rows("INBOX")[0]["is_unread"] == 0


def test_a_moved_mail_shows_in_its_new_folder(mailbox):
    mailbox.server.deliver("INBOX", "Wandert ins Archiv")
    mailbox.run_once()
    c = mailbox.server.client()
    c.select_folder("INBOX")
    c.move([1], "Archiv")
    mailbox.sync_pass()
    assert [(r["folder"], r["subject"]) for r in mailbox.rows()] == [("Archiv", "Wandert ins Archiv")]


def test_a_copy_in_two_folders_is_two_mails_and_stays_put(mailbox):
    raw_id = "doppelt@example.org"
    mailbox.server.deliver("INBOX", "Doppelt", message_id=raw_id)
    mailbox.server.deliver("Archiv", "Doppelt", message_id=raw_id)
    mailbox.scope("all")
    mailbox.sync_pass()
    second = mailbox.sync_pass()
    assert sorted(r["folder"] for r in mailbox.rows()) == ["Archiv", "INBOX"]
    assert second["fetched"] == 0                          # no hopping, nothing fetched again


def test_a_new_uidvalidity_keeps_the_mails(mailbox):
    for n in range(3):
        mailbox.server.deliver("INBOX", f"Mail {n}")
    mailbox.run_once()
    ids_before = sorted(r["id"] for r in mailbox.rows("INBOX"))
    inbox = mailbox.server.folders["INBOX"]
    inbox.validity += 1
    inbox.msgs = {u + 100: m for u, m in inbox.msgs.items()}
    inbox.next_uid = 104
    mailbox.run_once()
    rows = mailbox.rows("INBOX")
    assert sorted(r["id"] for r in rows) == ids_before and sorted(r["uid"] for r in rows) == [101, 102, 103]


# ─── deleting never loses a mail by accident ────────────────────────

def _delete(mailbox, subject):
    from backend import email_actions
    row = next(r for r in mailbox.rows() if r["subject"] == subject)
    return email_actions.delete_message(row["id"], mailbox.uid)


def _server_subjects(mailbox, folder):
    import re as _re
    return sorted(_re.search(rb"Subject: ([^\r]+)", m["raw"]).group(1).decode()
                  for m in mailbox.server.folders[folder].msgs.values())


def test_delete_moves_to_the_trash(mailbox):
    mailbox.server.deliver("INBOX", "Weg damit")
    mailbox.run_once()
    assert _delete(mailbox, "Weg damit")
    assert _server_subjects(mailbox, "INBOX") == [] and _server_subjects(mailbox, "Gelöscht") == ["Weg damit"]
    assert mailbox.server.plain_expunges == 0


@pytest.mark.parametrize("uidplus", [True, False])
def test_without_move_only_this_mail_is_removed(fresh_app, monkeypatch, uidplus):
    """COPY + UID EXPUNGE with UIDPLUS; without it the original stays marked
    \\Deleted. A plain EXPUNGE would also erase a mail another program had
    only marked as deleted."""
    server = FakeServer(move=False, uidplus=uidplus)
    from backend import credential_store, email_fetcher as F, email_actions
    from backend.database import get_conn
    _client, uid = login_client(fresh_app, role="member", name="Beate", email="beate2@example.test")
    monkeypatch.setattr(credential_store, "get", lambda key: {"imap_password": "pw"})
    monkeypatch.setattr(F, "IMAPClient", lambda *a, **k: server.client())
    monkeypatch.setattr(email_actions, "IMAPClient", lambda *a, **k: server.client())
    monkeypatch.setattr(F, "POLL_FALLBACK_S", 0)
    with get_conn() as conn:
        aid = conn.execute(
            "INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_port, imap_username, smtp_host, "
            "smtp_port, smtp_username, credential_key) VALUES (?, 'b2@example.test', 'i', 993, 'b', 's', 465, 'b', 'k') "
            "RETURNING id", (uid,)).fetchone()["id"]
        conn.commit()
    server.deliver("INBOX", "Weg damit")
    server.deliver("INBOX", "Auf dem Handy als gelöscht markiert")
    F._run_once(F._load_account_config(aid))
    server.folders["INBOX"].msgs[2]["flags"].add(b"\\Deleted")
    with get_conn() as conn:
        mid = conn.execute("SELECT id FROM email_messages WHERE account_id=? AND subject='Weg damit'", (aid,)).fetchone()["id"]
    assert email_actions.delete_message(mid, uid)
    inbox = server.folders["INBOX"].msgs
    assert server.plain_expunges == 0 and 2 in inbox                # the other one is untouched
    assert (1 not in inbox) if uidplus else (b"\\Deleted" in inbox[1]["flags"])
    assert len(server.folders["Gelöscht"].msgs) == 1


def test_no_trash_folder_means_nothing_is_deleted(mailbox):
    mailbox.server.folders.pop("Gelöscht")
    mailbox.server.deliver("INBOX", "Bleibt")
    mailbox.run_once()
    assert not _delete(mailbox, "Bleibt")
    assert _server_subjects(mailbox, "INBOX") == ["Bleibt"] and [r["subject"] for r in mailbox.rows()] == ["Bleibt"]
    r = mailbox.http.delete(f"/api/email/messages/{mailbox.rows()[0]['id']}")
    assert r.status_code == 502 and "nothing was deleted" in r.json()["detail"]


def test_a_refusing_server_deletes_nothing(mailbox, monkeypatch):
    mailbox.server.deliver("INBOX", "Bleibt auch")
    mailbox.run_once()
    from tests.fake_imap import FakeClient
    monkeypatch.setattr(FakeClient, "move", lambda self, *a: (_ for _ in ()).throw(OSError("connection reset")))
    monkeypatch.setattr(FakeClient, "copy", lambda self, *a: (_ for _ in ()).throw(OSError("connection reset")))
    assert not _delete(mailbox, "Bleibt auch")
    assert _server_subjects(mailbox, "INBOX") == ["Bleibt auch"] and len(mailbox.rows("INBOX")) == 1


def test_deleting_in_the_trash_is_for_good_and_only_that_mail(mailbox):
    mailbox.server.deliver("Gelöscht", "Endgültig weg")
    mailbox.server.deliver("Gelöscht", "Anderswo als gelöscht markiert")
    mailbox.run_once()
    mailbox.server.folders["Gelöscht"].msgs[2]["flags"].add(b"\\Deleted")
    assert _delete(mailbox, "Endgültig weg")
    assert list(mailbox.server.folders["Gelöscht"].msgs) == [2] and mailbox.server.plain_expunges == 0


def test_a_mail_taken_back_out_of_the_trash_shows_again(mailbox):
    mailbox.server.deliver("INBOX", "Doch noch gebraucht")
    mailbox.run_once()
    assert _delete(mailbox, "Doch noch gebraucht")
    mailbox.sync_pass()
    c = mailbox.server.client()                            # on the phone: back to the inbox
    c.select_folder("Gelöscht")
    c.move(list(mailbox.server.folders["Gelöscht"].msgs), "INBOX")
    mailbox.sync_pass()
    assert [(r["folder"], r["subject"]) for r in mailbox.rows()] == [("INBOX", "Doch noch gebraucht")]


def test_bulk_delete_without_trash_deletes_nothing(mailbox):
    from backend import email_actions
    mailbox.server.folders.pop("Gelöscht")
    for n in range(3):
        mailbox.server.deliver("INBOX", f"Mail {n}")
    mailbox.run_once()
    out = email_actions.delete_messages_bulk([r["id"] for r in mailbox.rows()], mailbox.uid)
    assert out["deleted"] == 0 and out["failed"] == 3
    assert len(mailbox.server.folders["INBOX"].msgs) == 3 and len(mailbox.rows()) == 3


def test_a_server_without_folder_flags(mailbox):
    """Many servers name their folders but do not flag them (SPECIAL-USE).
    The Trash and Sent folders must still stay out of the inbox view, and
    deleting must still find the Trash."""
    from tests.fake_imap import Folder
    s = mailbox.server
    s.folders = {"INBOX": Folder("INBOX"), "Papierkorb": Folder("Papierkorb"), "Gesendet": Folder("Gesendet")}
    s.deliver("INBOX", "Bleibt im Posteingang")
    s.deliver("INBOX", "Kommt in den Papierkorb")
    s.deliver("Gesendet", "Selbst geschrieben")
    mailbox.run_once()
    assert _delete(mailbox, "Kommt in den Papierkorb")
    mailbox.sync_pass()
    inbox_view = [m["subject"] for m in mailbox.http.get("/api/email/messages").json()]
    assert inbox_view == ["Bleibt im Posteingang"]
    assert _server_subjects(mailbox, "Papierkorb") == ["Kommt in den Papierkorb"]


def _gmail(mailbox):
    from tests.fake_imap import Folder
    s = mailbox.server
    s.folders = {"INBOX": Folder("INBOX"),
                 "[Gmail]/All Mail": Folder("[Gmail]/All Mail", (b"\\All",)),
                 "[Gmail]/Trash": Folder("[Gmail]/Trash", (b"\\Trash",))}
    return s


def _both(server, subject, mid):
    """A Gmail mail: in the inbox and, as a copy, in All Mail."""
    server.deliver("INBOX", subject, message_id=mid)
    server.deliver("[Gmail]/All Mail", subject, message_id=mid)


def test_gmail_all_mail_does_not_double_the_inbox(mailbox):
    s = _gmail(mailbox)
    for n in range(3):
        _both(s, f"Mail {n}", f"g{n}@example.org")
    mailbox.run_once()
    mailbox.sync_pass()
    assert sorted((r["folder"], r["subject"]) for r in mailbox.rows()) == [("INBOX", f"Mail {n}") for n in range(3)]


def test_gmail_archiving_keeps_the_mail(mailbox):
    s = _gmail(mailbox)
    _both(s, "Wird archiviert", "arch@example.org")
    mailbox.run_once()
    s.folders["INBOX"].msgs.clear()                       # "Archive" in Gmail: out of the inbox only
    mailbox.sync_pass()
    assert [(r["folder"], r["subject"]) for r in mailbox.rows()] == [("[Gmail]/All Mail", "Wird archiviert")]


def test_gmail_rows_that_hopped_to_all_mail_come_back_to_the_inbox(mailbox):
    """The old matching left inbox mail filed under All Mail (472 of them
    on the workstation); the pass puts each back in its real folder."""
    s = _gmail(mailbox)
    _both(s, "Gehört in den Posteingang", "hop@example.org")
    mailbox.run_once()
    from backend.database import get_conn
    with get_conn() as conn:                              # the state the old code left behind
        conn.execute("UPDATE email_messages SET folder_id=?, uid=1 WHERE account_id=?",
                     (mailbox.folder_id("[Gmail]/All Mail"), mailbox.aid))
        conn.commit()
    ids_before = [r["id"] for r in mailbox.rows()]
    mailbox.sync_pass()
    rows = mailbox.rows()
    assert [(r["folder"], r["subject"]) for r in rows] == [("INBOX", "Gehört in den Posteingang")]
    assert [r["id"] for r in rows] == ids_before          # the same row, drafts and category kept
