"""Pipelines, stage 1: follow up on a sent mail until the answer comes.

The rules under test (docs/plans/2026-09-25-pipelines.md):
  * a pipeline is its owner's alone — others get 404, admins included
  * the answer is looked for beyond the thread: other addresses of the
    same company, a domain carrying the company's name, a number from
    the first mail, every folder
  * only "sicher nicht" with fresh mail lets a reminder go; a possible
    answer or a stale mailbox goes to the person
  * a send is claimed with a fixed key first; a crash in the middle is
    asked about, never repeated
  * parents/admins can switch pipelines off per person
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import login_client


def _utc(days_ago: float = 0, hours_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def _account(owner: str, email: str, sync_minutes_ago: int = 0) -> int:
    from backend.database import conn_ctx
    with conn_ctx() as c:
        cur = c.execute(
            "INSERT INTO email_accounts (owner_user_id, email, imap_host, imap_username, smtp_host, "
            "smtp_username, credential_key, last_sync_at) VALUES (?, ?, 'imap.invalid', ?, "
            "'smtp.invalid', ?, 'unused', to_char(now() - (? * interval '1 minute'), "
            "'YYYY-MM-DD HH24:MI:SS')) RETURNING id",
            (owner, email, email, email, sync_minutes_ago),
        )
        return int(cur.fetchone()["id"])


_uid_counter = [1000]


def _mail(owner: str, account: int, *, sent: bool = False, frm: str = "", to=None, subject: str = "",
          body: str = "", message_id: str = "", in_reply_to: str = "", received: str = "") -> int:
    from backend.database import conn_ctx
    _uid_counter[0] += 1
    with conn_ctx() as c:
        cur = c.execute(
            "INSERT INTO email_messages (account_id, uid, message_id, in_reply_to, thread_id, from_email, "
            "to_addrs, subject, snippet, body_text, date_received, is_sent, owner_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (account, _uid_counter[0], message_id or f"m{_uid_counter[0]}@test", in_reply_to or None,
             in_reply_to or message_id or f"m{_uid_counter[0]}@test", frm,
             json.dumps([{"email": a} for a in (to or [])]), subject, body[:200], body,
             received or _utc(), 1 if sent else 0, owner),
        )
        return int(cur.fetchone()["id"])


@pytest.fixture
def home(fresh_app, monkeypatch):
    # The model is never called from tests; without a fake it is "not reachable".
    from backend.pipelines import writer

    def _no_model(*a, **kw):
        raise ConnectionError("no model in tests")
    monkeypatch.setattr(writer, "_complete", _no_model)
    dirk_c, dirk = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    beate_c, beate = login_client(fresh_app, role="member", name="Beate", email="b@example.local")
    kid_c, kid = login_client(fresh_app, role="restricted", name="Kind", email="k@example.local")
    acct = _account(dirk, "dirk@example.org")
    origin = _mail(dirk, acct, sent=True, frm="dirk@example.org", to=["kuendigung@stadtwerke-muster.de"],
                   subject="Kündigung Stromvertrag", message_id="orig-1@example.org",
                   body="Hiermit kündige ich den Vertrag, Kundennummer 4711-0815-22, zum nächstmöglichen Termin.",
                   received=_utc(days_ago=6))
    return {"app": fresh_app, "dirk_c": dirk_c, "dirk": dirk, "beate_c": beate_c, "beate": beate,
            "kid_c": kid_c, "kid": kid, "acct": acct, "origin": origin}


def _create(h) -> dict:
    r = h["dirk_c"].post("/api/pipelines", json={"mail_id": h["origin"]})
    assert r.status_code == 201, r.text
    return r.json()


def _approve_all_and_start(h, p) -> dict:
    for s in p["steps"]:
        if s["action"] == "mail_senden":
            r = h["dirk_c"].post(f"/api/pipelines/{p['id']}/steps/{s['id']}/approve", json={"approved": True})
            assert r.status_code == 200, r.text
    r = h["dirk_c"].post(f"/api/pipelines/{p['id']}/start")
    assert r.status_code == 200, r.text
    return r.json()


# ─── create, features, ownership ────────────────────────────────────

def test_draft_from_sent_mail_knows_what_to_look_for(home):
    p = _create(home)
    assert p["state"] == "entwurf"
    f = p["features"]
    assert f["addresses"] == ["kuendigung@stadtwerke-muster.de"]
    assert f["domains"] == ["stadtwerke-muster.de"]
    assert f["names"] == ["stadtwerke-muster"]
    assert "4711-0815-22" in f["numbers"]
    assert [s["action"] for s in p["steps"]] == ["mail_senden", "mail_senden", "uebergabe"]
    assert p["steps"][0]["payload"]["subject"] == "Re: Kündigung Stromvertrag"
    assert p["steps"][0]["payload"]["to"] == ["kuendigung@stadtwerke-muster.de"]


def test_only_a_sent_mail_can_be_followed(home):
    inbound = _mail(home["dirk"], home["acct"], frm="x@y.de", subject="Hallo")
    r = home["dirk_c"].post("/api/pipelines", json={"mail_id": inbound})
    assert r.status_code == 400


def test_someone_elses_pipeline_is_a_404_admin_included(home):
    p = _create(home)
    assert home["beate_c"].get(f"/api/pipelines/{p['id']}").status_code == 404
    assert home["beate_c"].get("/api/pipelines").json() == []
    assert home["beate_c"].post(f"/api/pipelines/{p['id']}/cancel").status_code == 404
    # Beate's mail is not Dirk's to follow either — not even as platform admin.
    b_acct = _account(home["beate"], "beate@example.org")
    b_mail = _mail(home["beate"], b_acct, sent=True, frm="beate@example.org", to=["a@b.de"], subject="x")
    assert home["dirk_c"].post("/api/pipelines", json={"mail_id": b_mail}).status_code == 404


def test_start_needs_every_mail_approved_and_editing_clears_approval(home):
    p = _create(home)
    pid = p["id"]
    assert home["dirk_c"].post(f"/api/pipelines/{pid}/start").status_code == 409
    for s in p["steps"][:2]:
        home["dirk_c"].post(f"/api/pipelines/{pid}/steps/{s['id']}/approve", json={"approved": True})
    steps = home["dirk_c"].get(f"/api/pipelines/{pid}").json()["steps"]
    assert steps[0]["approved"] and steps[1]["approved"]
    # change the second text: only that one loses its approval
    body = [{"action": s["action"], "after_days": s["after_days"], "payload": s["payload"]} for s in steps]
    body[1]["payload"] = dict(body[1]["payload"], body="Anderer Text")
    steps = home["dirk_c"].put(f"/api/pipelines/{pid}/steps", json={"steps": body}).json()["steps"]
    assert steps[0]["approved"] is True
    assert steps[1]["approved"] is False
    assert home["dirk_c"].post(f"/api/pipelines/{pid}/start").status_code == 409


# ─── recognising the answer ─────────────────────────────────────────

def _check(pid: int) -> dict:
    from backend.pipelines import engine, store
    p = store.get(pid, None)
    return engine.kind_of(p).check(p)


def test_nothing_came_is_sicher_nicht(home):
    p = _create(home)
    _mail(home["dirk"], home["acct"], frm="news@shop.example", subject="Angebote", body="50% auf alles")
    chk = _check(p["id"])
    assert chk["result"] == "sicher_nicht"
    assert chk["candidates"] == []


def test_answer_from_another_address_of_the_same_company_is_found(home):
    p = _create(home)
    _mail(home["dirk"], home["acct"], frm="noreply@kundenservice.stadtwerke-muster.de",
          subject="Ihre Nachricht", body="Wir haben Ihre Nachricht erhalten.")
    chk = _check(p["id"])
    assert chk["result"] == "vielleicht"
    assert "von der Domain" in chk["candidates"][0]["why"][0]


def test_answer_from_a_different_domain_carrying_the_name_is_found(home):
    p = _create(home)
    _mail(home["dirk"], home["acct"], frm="service@stadtwerke-muster-energie.com",
          subject="Bestätigung", body="Ihre Kündigung ist bestätigt.")
    assert _check(p["id"])["result"] == "vielleicht"


def test_answer_quoting_the_customer_number_is_found_from_any_sender(home):
    p = _create(home)
    _mail(home["dirk"], home["acct"], frm="ticket@helpdesk-system.example",
          subject="Vorgang", body="Betrifft Kundennummer 4711-0815-22: erledigt.")
    chk = _check(p["id"])
    assert chk["result"] == "vielleicht"
    assert any("4711-0815-22" in w for w in chk["candidates"][0]["why"])


def test_answer_in_the_thread_is_found(home):
    p = _create(home)
    _mail(home["dirk"], home["acct"], frm="someone@elsewhere.example", subject="Re: Kündigung",
          in_reply_to="orig-1@example.org")
    assert _check(p["id"])["result"] == "vielleicht"


def test_mail_from_before_the_first_message_does_not_count(home):
    p = _create(home)
    _mail(home["dirk"], home["acct"], frm="info@stadtwerke-muster.de", subject="Rechnung",
          received=_utc(days_ago=30))
    assert _check(p["id"])["result"] == "sicher_nicht"


def test_stale_mailbox_cannot_check(home):
    p = _create(home)
    from backend.database import conn_ctx
    with conn_ctx() as c:
        c.execute("UPDATE email_accounts SET last_sync_at = to_char(now() - interval '3 hours', "
                  "'YYYY-MM-DD HH24:MI:SS') WHERE id = ?", (home["acct"],))
    chk = _check(p["id"])
    assert chk["result"] == "kann_nicht_pruefen"
    assert "zuletzt vor" in chk["problems"][0]


def test_unreadable_mail_since_the_start_cannot_check(home):
    p = _create(home)
    from backend.database import conn_ctx
    with conn_ctx() as c:
        c.execute("INSERT INTO email_fetch_failures (account_id, folder_id, uid, last_error) "
                  "VALUES (?, 1, 99, 'parse error')", (home["acct"],))
    chk = _check(p["id"])
    assert chk["result"] == "kann_nicht_pruefen"
    assert "nicht gelesen" in chk["problems"][0]


def test_own_mail_to_the_other_side_means_the_person_took_over(home):
    p = _create(home)
    _mail(home["dirk"], home["acct"], sent=True, frm="dirk@example.org",
          to=["kuendigung@stadtwerke-muster.de"], subject="Nachtrag")
    assert _check(p["id"])["took_over"]


# ─── the engine ─────────────────────────────────────────────────────

def _process(pid: int) -> dict:
    from backend.pipelines import engine, store
    engine.process(pid)
    return store.get(pid, None)


def test_due_step_with_nothing_found_asks_the_person(home):
    p = _approve_all_and_start(home, _create(home))
    from backend.pipelines import engine
    from unittest.mock import patch
    with patch.object(engine, "in_window", return_value=True):
        after = _process(p["id"])
    assert after["attention"] == "schritt_faellig"
    from backend.database import conn_ctx
    with conn_ctx() as c:
        n = c.execute("SELECT count(*) AS n FROM notifications WHERE user_id = ? AND kind = 'pipeline'",
                      (home["dirk"],)).fetchone()["n"]
    assert int(n) == 1


def test_step_not_yet_due_waits_quietly(home):
    from backend.database import conn_ctx
    with conn_ctx() as c:
        c.execute("UPDATE email_messages SET date_received = ? WHERE id = ?", (_utc(days_ago=1), home["origin"]))
    p = _approve_all_and_start(home, _create(home))
    after = _process(p["id"])
    assert after["attention"] is None
    assert after["next_run_at"] is not None


def test_possible_answer_stops_the_pipeline_and_asks(home):
    p = _approve_all_and_start(home, _create(home))
    _mail(home["dirk"], home["acct"], frm="info@stadtwerke-muster.de", subject="Eingang")
    after = _process(p["id"])
    assert after["attention"] == "vielleicht"
    assert after["attention_detail"]["candidates"]


def _fake_send(calls):
    def send(**kw):
        calls.append(kw)
        from backend import email_sender
        email_sender.store_sent_mirror(
            account_id=kw["account_id"], owner_user_id=OWNER[0], from_email="dirk@example.org", from_name="",
            message_id=kw["message_id"], to=kw["to"], cc=None, subject=kw["subject"],
            body_text=kw["body_text"], body_html=None, in_reply_to=kw["in_reply_to"],
            references=kw["references"])
        return {"ok": True, "message_id": kw["message_id"]}
    return send


OWNER = [""]


def test_person_sends_the_due_reminder_as_a_reply_in_the_thread(home):
    p = _approve_all_and_start(home, _create(home))
    OWNER[0] = home["dirk"]
    calls = []
    from unittest.mock import patch
    from backend import email_sender
    step = p["steps"][0]
    with patch.object(email_sender, "send", side_effect=_fake_send(calls)):
        r = home["dirk_c"].post(f"/api/pipelines/{p['id']}/steps/{step['id']}/send", json={})
    assert r.status_code == 200, r.text
    assert len(calls) == 1
    assert calls[0]["in_reply_to"] == "orig-1@example.org"
    assert "orig-1@example.org" in calls[0]["references"]
    assert calls[0]["to"] == ["kuendigung@stadtwerke-muster.de"]
    assert "> Hiermit kündige ich" in calls[0]["body_text"]
    d = r.json()
    assert d["steps"][0]["status"] == "erledigt"
    # the same step cannot be sent again
    with patch.object(email_sender, "send", side_effect=_fake_send(calls)):
        again = home["dirk_c"].post(f"/api/pipelines/{p['id']}/steps/{step['id']}/send", json={})
    assert again.status_code == 409
    assert len(calls) == 1
    # our own reminder is not mistaken for an answer or for the person taking over
    chk = _check(p["id"])
    assert chk["candidates"] == [] and chk["took_over"] == []


def test_send_is_refused_when_a_possible_answer_arrived_meanwhile(home):
    p = _approve_all_and_start(home, _create(home))
    _mail(home["dirk"], home["acct"], frm="kuendigung@stadtwerke-muster.de", subject="Re: Kündigung")
    calls = []
    from unittest.mock import patch
    from backend import email_sender
    with patch.object(email_sender, "send", side_effect=_fake_send(calls)):
        r = home["dirk_c"].post(f"/api/pipelines/{p['id']}/steps/{p['steps'][0]['id']}/send", json={})
    assert r.status_code == 409
    assert calls == []
    assert home["dirk_c"].get(f"/api/pipelines/{p['id']}").json()["attention"] == "vielleicht"


def test_send_is_refused_on_a_stale_mailbox_unless_the_person_insists(home):
    p = _approve_all_and_start(home, _create(home))
    OWNER[0] = home["dirk"]
    from backend.database import conn_ctx
    with conn_ctx() as c:
        c.execute("UPDATE email_accounts SET last_sync_at = to_char(now() - interval '3 hours', "
                  "'YYYY-MM-DD HH24:MI:SS') WHERE id = ?", (home["acct"],))
    calls = []
    from unittest.mock import patch
    from backend import email_sender
    url = f"/api/pipelines/{p['id']}/steps/{p['steps'][0]['id']}/send"
    with patch.object(email_sender, "send", side_effect=_fake_send(calls)):
        assert home["dirk_c"].post(url, json={}).status_code == 409
        assert calls == []
        assert home["dirk_c"].post(url, json={"despite_stale": True}).status_code == 200
    assert len(calls) == 1


def test_person_confirms_or_dismisses_a_possible_answer(home):
    p = _approve_all_and_start(home, _create(home))
    ack = _mail(home["dirk"], home["acct"], frm="info@stadtwerke-muster.de", subject="Eingangsbestätigung")
    _process(p["id"])
    r = home["dirk_c"].post(f"/api/pipelines/{p['id']}/answer", json={"mail_id": ack, "is_answer": False})
    assert r.status_code == 200
    assert r.json()["attention"] is None
    assert _check(p["id"])["candidates"] == []
    real = _mail(home["dirk"], home["acct"], frm="info@stadtwerke-muster.de", subject="Kündigungsbestätigung")
    _process(p["id"])
    r = home["dirk_c"].post(f"/api/pipelines/{p['id']}/answer", json={"mail_id": real, "is_answer": True})
    assert r.json()["state"] == "erledigt"
    assert r.json()["result"]["answer_mail_id"] == real


def test_a_send_interrupted_by_a_crash_is_asked_about_not_repeated(home):
    p = _approve_all_and_start(home, _create(home))
    from backend.pipelines import engine, store
    from backend.database import conn_ctx
    step = p["steps"][0]
    assert store.action_begin(p["id"], step["id"], "k1", "mail", "lost-1@example.org")
    assert not store.action_begin(p["id"], step["id"], "k1", "mail", "lost-1@example.org")
    with conn_ctx() as c:
        c.execute("UPDATE pipeline_actions SET created_at = now() - interval '1 hour' WHERE idem_key = 'k1'")
    engine.recover_stuck_sends()
    after = store.get(p["id"], None)
    assert after["attention"] == "versand_unklar"
    assert store.actions(p["id"])[0]["status"] == "unklar"


def test_a_send_interrupted_after_the_mail_went_out_is_recorded_as_sent(home):
    p = _approve_all_and_start(home, _create(home))
    from backend.pipelines import engine, store
    from backend.database import conn_ctx
    step = p["steps"][0]
    store.action_begin(p["id"], step["id"], "k2", "mail", "went-1@example.org")
    _mail(home["dirk"], home["acct"], sent=True, frm="dirk@example.org", to=["x@y.de"],
          message_id="went-1@example.org")
    with conn_ctx() as c:
        c.execute("UPDATE pipeline_actions SET created_at = now() - interval '1 hour' WHERE idem_key = 'k2'")
    engine.recover_stuck_sends()
    assert store.actions(p["id"])[0]["status"] == "gesendet"
    assert store.steps(p["id"])[0]["status"] == "erledigt"


def test_new_mail_pokes_waiting_pipelines(home):
    p = _approve_all_and_start(home, _create(home))
    from backend.pipelines import store, poke
    store.update(p["id"], next_run_at=store.now() + timedelta(days=3))
    poke(home["dirk"])
    assert store.to_dt(store.get(p["id"], None)["next_run_at"]) <= store.now()


def test_send_window():
    from backend.pipelines import engine
    local = datetime(2026, 9, 26, 10, 0).astimezone()   # a Saturday
    assert engine.in_window({"send_days": "alle", "send_from_hour": 8, "send_to_hour": 20}, local)
    assert not engine.in_window({"send_days": "werktags", "send_from_hour": 8, "send_to_hour": 20}, local)
    nxt = engine.next_window({"send_days": "werktags", "send_from_hour": 8, "send_to_hour": 20}, local)
    assert nxt.astimezone().weekday() == 0 and nxt.astimezone().hour == 8
    night = datetime(2026, 9, 24, 23, 0).astimezone()
    assert not engine.in_window({"send_days": "alle", "send_from_hour": 8, "send_to_hour": 20}, night)


# ─── the per-person switch ──────────────────────────────────────────

def test_parent_switches_a_child_off_and_the_child_cannot(home):
    kid, beate_c, kid_c = home["kid"], home["beate_c"], home["kid_c"]
    assert kid_c.get("/api/pipelines/people").status_code == 403
    assert kid_c.put(f"/api/pipelines/people/{home['beate']}", json={"enabled": False}).status_code == 403
    assert beate_c.put(f"/api/pipelines/people/{kid}", json={"enabled": False}).status_code == 200
    assert kid_c.get("/api/pipelines/me").json()["enabled"] is False
    people = {x["id"]: x for x in beate_c.get("/api/pipelines/people").json()}
    assert people[kid]["enabled"] is False and people[kid]["changed_by"] == "Beate"


def test_a_member_cannot_switch_an_admin_off(home):
    r = home["beate_c"].put(f"/api/pipelines/people/{home['dirk']}", json={"enabled": False})
    assert r.status_code == 403


def test_switched_off_person_cannot_create_and_running_ones_hold(home):
    p = _approve_all_and_start(home, _create(home))
    from backend.pipelines import store
    store.set_person_enabled(home["dirk"], False, home["beate"])
    assert home["dirk_c"].post("/api/pipelines", json={"mail_id": home["origin"]}).status_code == 403
    assert store.get(p["id"], None)["attention"] == "person_aus"
    after = _process(p["id"])
    assert after["attention"] == "person_aus"
    store.set_person_enabled(home["dirk"], True, home["beate"])
    assert store.get(p["id"], None)["attention"] is None


def test_switching_off_keeps_an_open_question_so_nothing_is_sent_twice(home):
    p = _approve_all_and_start(home, _create(home))
    from backend.pipelines import store
    store.update(p["id"], attention="versand_unklar", attention_json={"step_id": p["steps"][0]["id"]})
    store.set_person_enabled(home["dirk"], False, home["beate"])
    assert _process(p["id"])["attention"] == "versand_unklar"
    store.set_person_enabled(home["dirk"], True, home["beate"])
    assert store.get(p["id"], None)["attention"] == "versand_unklar"


def test_saving_steps_keeps_their_ids(home):
    p = _create(home)
    before = [s["id"] for s in p["steps"]]
    body = [{"action": s["action"], "after_days": s["after_days"] + 1, "payload": s["payload"]} for s in p["steps"]]
    after = home["dirk_c"].put(f"/api/pipelines/{p['id']}/steps", json={"steps": body}).json()["steps"]
    assert [s["id"] for s in after] == before
    assert [s["after_days"] for s in after] == [6, 8, 8]
    shorter = home["dirk_c"].put(f"/api/pipelines/{p['id']}/steps", json={"steps": body[1:]}).json()["steps"]
    assert len(shorter) == 2 and shorter[-1]["action"] == "uebergabe"


def test_an_instant_answer_stamped_to_the_second_before_our_send_is_still_found(home):
    """Mail servers stamp arrival to the whole second; an auto-reply in the
    same second can look a fraction older than our own send."""
    p = _create(home)
    from backend.pipelines import store
    since = store.to_dt(store.get(p["id"], None)["since_at"])
    _mail(home["dirk"], home["acct"], frm="kuendigung@stadtwerke-muster.de", subject="Automatische Antwort",
          received=(since - timedelta(seconds=1)).replace(microsecond=0).isoformat())
    assert _check(p["id"])["result"] == "vielleicht"


def test_a_member_sees_an_admins_switch_but_cannot_flip_it(home):
    people = {x["id"]: x for x in home["beate_c"].get("/api/pipelines/people").json()}
    assert people[home["dirk"]]["can_change"] is False
    assert people[home["kid"]]["can_change"] is True
    assert {x["id"]: x for x in home["dirk_c"].get("/api/pipelines/people").json()}[home["dirk"]]["can_change"]


# ─── the model writes the reminders ─────────────────────────────────

PLAN = {"kind": "kuendigung", "goal": "Kündigungsbestätigung mit Datum des Vertragsendes",
        "reminders": [
            {"after_days": 10, "why": "Stadtwerke brauchen oft zwei Wochen", "subject": "Re: Kündigung Stromvertrag",
             "body": "Sehr geehrte Damen und Herren,\n\nich bitte um Bestätigung meiner Kündigung (Kundennummer 4711-0815-22).\n\nDirk"},
            {"after_days": 7, "why": "zweite, bestimmtere Nachfrage", "subject": "Re: Kündigung Stromvertrag",
             "body": "Bitte bestätigen Sie bis zum Monatsende.\n\nDirk"}],
        "handover_days": 5}


def test_the_model_plans_the_days_and_writes_the_reminders(home, monkeypatch):
    from backend.pipelines import writer
    prompts = []
    monkeypatch.setattr(writer, "_complete", lambda prompt, **kw: prompts.append(prompt) or json.dumps(PLAN))
    p = _create(home)   # the background task runs before TestClient returns
    d = home["dirk_c"].get(f"/api/pipelines/{p['id']}").json()
    assert d["config"]["drafting"] is False
    assert d["goal"] == PLAN["goal"]
    assert [s["after_days"] for s in d["steps"]] == [10, 7, 5]
    assert d["steps"][0]["payload"]["source"] == "llm"
    assert d["steps"][0]["payload"]["why"] == "Stadtwerke brauchen oft zwei Wochen"
    assert d["steps"][0]["payload"]["to"] == ["kuendigung@stadtwerke-muster.de"]
    assert "4711-0815-22" in prompts[0] and "Kündigung Stromvertrag" in prompts[0]


def test_without_the_model_the_template_stays_and_says_so(home):
    p = _create(home)
    d = home["dirk_c"].get(f"/api/pipelines/{p['id']}").json()
    assert d["config"]["drafting"] is False
    assert d["steps"][0]["payload"]["source"] == "vorlage"
    assert any("nicht erreichbar" in e["text"] for e in d["events"])


def test_a_due_reminder_is_written_afresh_and_needs_approval_of_that_text(home, monkeypatch):
    from backend.pipelines import engine, writer
    from unittest.mock import patch
    p = _approve_all_and_start(home, _create(home))
    ack = _mail(home["dirk"], home["acct"], frm="info@stadtwerke-muster.de", subject="Eingangsbestätigung",
                body="Wir haben Ihre Nachricht erhalten.")
    _process(p["id"])
    home["dirk_c"].post(f"/api/pipelines/{p['id']}/answer", json={"mail_id": ack, "is_answer": False})
    prompts = []
    fresh = {"subject": "Re: Kündigung Stromvertrag",
             "body": "Sie haben den Eingang bestätigt, die Bestätigung der Kündigung fehlt noch.\n\nDirk"}
    monkeypatch.setattr(writer, "_complete", lambda prompt, **kw: prompts.append(prompt) or json.dumps(fresh))
    with patch.object(engine, "in_window", return_value=True):
        after = _process(p["id"])
        assert after["attention"] == "schritt_faellig"
        _process(p["id"])   # already written for today: not again
    assert len(prompts) == 1
    assert "Eingangsbestätigung" in prompts[0]
    step = home["dirk_c"].get(f"/api/pipelines/{p['id']}").json()["steps"][0]
    assert step["payload"]["source"] == "llm_frisch" and step["approved"] is False

    OWNER[0] = home["dirk"]
    calls = []
    from backend import email_sender
    url = f"/api/pipelines/{p['id']}/steps/{step['id']}/send"
    with patch.object(email_sender, "send", side_effect=_fake_send(calls)):
        assert home["dirk_c"].post(url, json={}).status_code == 409          # not approved
        stale = home["dirk_c"].post(url, json={"approve": True, "seen_body": "ein anderer Text"})
        assert stale.status_code == 409                                     # approves only what was seen
        ok = home["dirk_c"].post(url, json={"approve": True, "seen_body": fresh["body"]})
    assert ok.status_code == 200, ok.text
    assert calls[0]["body_text"].startswith("Sie haben den Eingang bestätigt")


def test_steps_cannot_be_changed_or_started_while_yorik_writes(home):
    p = _create(home)
    from backend.pipelines import store
    store.update(p["id"], config_json=dict(p["config"], drafting=True))
    body = [{"action": s["action"], "after_days": s["after_days"], "payload": s["payload"]} for s in p["steps"]]
    assert home["dirk_c"].put(f"/api/pipelines/{p['id']}/steps", json={"steps": body}).status_code == 409
    assert home["dirk_c"].post(f"/api/pipelines/{p['id']}/start").status_code == 409
    assert home["dirk_c"].post(f"/api/pipelines/{p['id']}/steps/{p['steps'][0]['id']}/approve",
                               json={"approved": True}).status_code == 409
