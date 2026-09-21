"""A subject taken over from a folded header must not break sending
(HTTP 500, 2026-09-21), and no header value may carry a line break."""

from backend import email_sender as S


class _Smtp:
    sent = []

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def starttls(self, **k): pass
    def login(self, *a): pass
    def send_message(self, msg, from_addr=None, to_addrs=None): _Smtp.sent.append((msg, list(to_addrs)))


def test_folded_subject_and_injection_are_flattened(monkeypatch):
    _Smtp.sent = []
    monkeypatch.setattr(S, "_load_account", lambda account_id: {
        "email": "dirk@example.org", "display_name": "Dirk\nWiniecki", "credential_key": "k", "smtp_host": "smtp.example.org",
        "smtp_port": 587, "smtp_ssl": 0, "smtp_starttls": 1, "smtp_username": "dirk", "id": 1})
    monkeypatch.setattr(S.credential_store, "get", lambda key: {"smtp_password": "pw"})
    monkeypatch.setattr(S.smtplib, "SMTP", _Smtp)
    stored = {}
    monkeypatch.setattr(S, "_imap_append_to_sent", lambda cfg, raw: None)
    monkeypatch.setattr(S, "_store_sent_copy", lambda cfg, mid, to, cc, subject, *a, **k: stored.update(subject=subject))

    result = S.send(account_id=1, to=["service@example.com\nBcc: evil@example.com"],
                    subject="Re: Einladung: Dirk Winiecki and\r\n Alessandro Leto Barone",
                    body_text="Hallo", in_reply_to="<abc@mail>\n", references=["<abc@mail>"])
    assert _Smtp.sent, result
    msg, rcpts = _Smtp.sent[0]
    assert msg["Subject"] == "Re: Einladung: Dirk Winiecki and Alessandro Leto Barone"
    assert "\n" not in msg["To"] and "\n" not in msg["From"] and msg["In-Reply-To"] == "<abc@mail>"
    assert stored["subject"] == msg["Subject"]                         # the sent copy is one line too
    assert msg["Bcc"] is None and len(rcpts) == 1                      # the injected header is just text in one address
