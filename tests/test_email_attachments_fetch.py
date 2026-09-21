"""Opening a mail asks for all its attachments at once: one IMAP fetch
per mail, and a PDF stays a PDF whatever the sender called its type."""

import threading

from backend import email_actions as E


def test_generic_type_follows_the_file_name():
    assert E.effective_mimetype("2. Mahnung.PDF", "application/octet-stream") == "application/pdf"
    assert E.effective_mimetype("Rechnungen.PDF", None) == "application/pdf"
    assert E.effective_mimetype("foto.JPG", "application/x-download") == "image/jpeg"
    assert E.effective_mimetype("brief.pdf", "application/pdf") == "application/pdf"
    assert E.effective_mimetype("daten.unbekannt", "application/octet-stream") == "application/octet-stream"
    assert E.effective_mimetype("x.pdf", "text/plain") == "text/plain"           # a specific label is believed


def test_one_fetch_per_mail_even_in_parallel(monkeypatch):
    E._RAW_CACHE.clear()
    logins = []

    class _Imap:
        def __enter__(self): logins.append(1); return self
        def __exit__(self, *a): return False
        _imap = type("S", (), {"sock": type("K", (), {"settimeout": staticmethod(lambda s: None)})()})()
        def select_folder(self, name, readonly=False): pass
        def fetch(self, uids, parts):
            import time; time.sleep(0.05)
            return {uids[0]: {b"BODY[]": b"raw message bytes"}}

    monkeypatch.setattr(E, "imap_for_account", lambda account_id: _Imap())
    out = []
    threads = [threading.Thread(target=lambda: out.append(E._raw_message(6116, 3, "INBOX", 34920))) for _ in range(15)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert out == [b"raw message bytes"] * 15 and len(logins) == 1
    E._raw_message(7000, 3, "INBOX", 1)
    assert len(logins) == 2                                                   # another mail, another fetch
