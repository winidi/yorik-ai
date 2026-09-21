"""Filing a mail attachment in Paperless asks who may see it, like the
chat card does. Until 2026-09-22 a member's attachment went up private
with no question, and (with the owner then taken by the workflow)
nobody saw it."""

from __future__ import annotations

from tests.conftest import login_client


class _Resp:
    ok, status_code, text = True, 200, '"task-1"'


def _attachment(conn, owner):
    aid = conn.execute("INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, smtp_username, credential_key) "
                       "VALUES (?, 'b@example.local', 'i', 'b', 's', 'b', 'k') RETURNING id", (owner,)).fetchone()["id"]
    mid = conn.execute("INSERT INTO email_messages (account_id, uid, owner_user_id, message_id, from_email, from_name, subject, body_text) "
                       "VALUES (?, 1, ?, '<m1@x>', 'k@example.org', 'Kommpact', 'Projektvertrag', '') RETURNING id", (aid, owner)).fetchone()["id"]
    return conn.execute("INSERT INTO email_attachments (message_id, filename, mimetype, size_bytes, paperless_state) "
                        "VALUES (?, 'vertrag.pdf', 'application/pdf', 10, 'suggested') RETURNING id", (mid,)).fetchone()["id"]


def test_filing_applies_the_chosen_visibility(fresh_app, monkeypatch):
    from backend import paperless_visibility as pv
    from backend.database import get_conn
    client, uid = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    with get_conn() as conn:
        att_id = _attachment(conn, uid); conn.commit()

    posted, applied = [], []
    monkeypatch.setattr("backend.external_users.get_user_paperless_creds", lambda user_id: {"base_url": "http://p", "api_key": "beate-token"})
    monkeypatch.setattr(pv, "resolve_visibility_tag_id", lambda level: {"shared": 7, "parents": 8}.get(level))
    monkeypatch.setattr(pv, "apply_after_consume", lambda task_id, vis, on_document=None: applied.append((task_id, vis)))
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: posted.append((url, kw["headers"], kw["data"])) or _Resp())
    monkeypatch.setattr("backend.email_actions.fetch_attachment_binary", lambda att_id, user_id: {"content": b"%PDF-1.4"})

    r = client.post(f"/api/email/attachments/{att_id}/paperless?visibility=shared")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "state": "filed", "visibility": "shared"}
    url, headers, data = posted[0]
    assert url == "http://p/api/documents/post_document/"
    assert headers["Authorization"] == "Token beate-token"          # her own token → she owns the document
    assert data["tags"] == [7]                                      # the visibility tag on the way in
    assert applied == [("task-1", "shared")]                        # the group's view permission after the consume
    with get_conn() as conn:
        row = conn.execute("SELECT paperless_state, paperless_visibility FROM email_attachments WHERE id = ?", (att_id,)).fetchone()
    assert (row["paperless_state"], row["paperless_visibility"]) == ("filed", "shared")


def test_without_an_answer_the_persons_default_applies(fresh_app, monkeypatch):
    from backend import paperless_visibility as pv
    from backend.database import get_conn
    client, uid = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    with get_conn() as conn:
        att_id = _attachment(conn, uid)
        conn.execute("UPDATE user_profiles SET default_doc_visibility = 'parents' WHERE id = ?", (uid,)); conn.commit()

    applied = []
    monkeypatch.setattr("backend.external_users.get_user_paperless_creds", lambda user_id: {"base_url": "http://p", "api_key": "t"})
    monkeypatch.setattr(pv, "resolve_visibility_tag_id", lambda level: None)
    monkeypatch.setattr(pv, "apply_after_consume", lambda task_id, vis, on_document=None: applied.append(vis))
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp())
    monkeypatch.setattr("backend.email_actions.fetch_attachment_binary", lambda att_id, user_id: {"content": b"%PDF-1.4"})

    assert client.post(f"/api/email/attachments/{att_id}/paperless").json()["visibility"] == "parents"
    assert applied == ["parents"]
    # "private" needs no permission step — Paperless's owner-only rule does it.
    with get_conn() as conn:
        conn.execute("UPDATE email_attachments SET paperless_state = 'suggested' WHERE id = ?", (att_id,)); conn.commit()
    assert client.post(f"/api/email/attachments/{att_id}/paperless?visibility=private").json()["visibility"] == "private"
    assert applied == ["parents"]
