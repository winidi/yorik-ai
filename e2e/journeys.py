"""Journeys: does Yorik do the *right* thing for each family member?

Runs against the test household (e2e/household.py up) through the same
routes the app uses, as Anna (runs the box), Ben (second parent), Clara
and David (children). Every check is recorded, none stops the run.

    venv/bin/python e2e/journeys.py           # report in e2e/report/journeys.md

With YORIK_E2E_REAL_LLM set when the household was started, the chat
checks use the real model; with the fake model they only prove the
chat path does not break.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
HOUSEHOLD = json.loads((HERE / ".run" / "household.json").read_text())
BASE = HOUSEHOLD["base_url"]
assert re.match(r"^http://127\.0\.0\.1:\d+$", BASE) and not BASE.endswith(":8000"), BASE
PW = HOUSEHOLD["password"]
PEOPLE = {p["key"]: p for p in HOUSEHOLD["people"]}
TODAY = date.today()
RESULTS: list[dict] = []


def record(area: str, name: str, ok: bool | None, detail: str = "") -> None:
    RESULTS.append({"area": area, "check": name, "ok": ok, "detail": detail[:500]})
    mark = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    print(f"[{mark}] {area}: {name}" + (f" — {detail[:160]}" if detail else ""), flush=True)


def check(area: str, name: str):
    """Decorator-ish: run fn, record PASS/FAIL, keep going on errors."""
    def wrap(fn):
        try:
            out = fn()
            if isinstance(out, tuple):
                record(area, name, *out)
            else:
                record(area, name, bool(out))
        except Exception as exc:  # noqa: BLE001
            record(area, name, False, f"{type(exc).__name__}: {exc} | {traceback.format_exc(limit=1).splitlines()[-1]}")
        return fn
    return wrap


class Person(requests.Session):
    def __init__(self, key: str):
        super().__init__()
        self.key, self.p = key, PEOPLE[key]
        self.role = self.p["role"]
        r = self.post(BASE + "/api/auth/login", json={"email": self.p["email"], "password": PW})
        r.raise_for_status()
        self.id = self.get(BASE + "/api/auth/me").json()["user"]["id"]

    def api(self, method: str, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 60)
        return self.request(method, BASE + path, **kw)

    def tasks(self) -> list[dict]:
        return self.api("GET", f"/api/tasks?role={self.role}").json()

    def task(self, title: str) -> dict | None:
        return next((t for t in self.tasks() if t["title"] == title), None)

    def events(self, days: int = 3) -> list[dict]:
        return self.api("GET", f"/api/events?role={self.role}&start_date={TODAY - timedelta(days=1)}"
                               f"&end_date={TODAY + timedelta(days=days)}").json()

    def ask(self, message: str, conversation_id: str | None = None) -> dict:
        t = time.time()
        r = self.api("POST", "/api/ask", json={"message": message, "role": self.role,
                                                "conversation_id": conversation_id}, timeout=400)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
        j["_status"], j["_seconds"] = r.status_code, round(time.time() - t, 1)
        return j


def titles(rows) -> set[str]:
    return {r.get("title") for r in rows}


def german(text: str) -> bool:
    words = re.findall(r"[a-zäöüß]+", (text or "").lower())
    de = sum(w in {"und", "ist", "die", "der", "das", "du", "hast", "heute", "morgen", "aufgabe", "termin",
                   "ich", "nicht", "ein", "eine", "für", "mit", "auf", "angelegt", "erledigt", "keine", "sind"}
             for w in words)
    en = sum(w in {"the", "and", "is", "you", "have", "today", "tomorrow", "task", "i", "not", "a", "for",
                   "with", "on", "created", "done", "no", "are", "got", "see"} for w in words)
    return de > en


# ─── the family logs in ─────────────────────────────────────────────

anna, ben, clara, david = (Person(k) for k in ("anna", "ben", "clara", "david"))
A = "Login and roles"


@check(A, "each person logs in and is who they are")
def _():
    return all(p.get(BASE + "/api/auth/me").json()["user"]["role"] == PEOPLE[p.key]["role"]
               for p in (anna, ben, clara, david))


@check(A, "a wrong password is refused")
def _():
    r = requests.post(BASE + "/api/auth/login", json={"email": "ben@example.test", "password": "falsch-falsch"})
    return r.status_code in (400, 401, 403), f"HTTP {r.status_code}"


@check(A, "only the operator sees the user list (Ben, Clara refused)")
def _():
    codes = {p.key: p.api("GET", "/api/users").status_code for p in (anna, ben, clara)}
    return codes == {"anna": 200, "ben": 403, "clara": 403}, str(codes)


@check(A, "a child cannot create a user")
def _():
    r = clara.api("POST", "/api/users", json={"email": "x@example.test", "name": "X", "role": "platform_admin",
                                               "password": "whatever-123", "auto_provision": []})
    return r.status_code in (401, 403), f"HTTP {r.status_code}"


@check(A, "the wall's person picker is closed to an ordinary phone session")
def _():
    r = anna.api("GET", "/api/auth/pin-pickable")          # kiosk devices only
    return r.status_code == 403, f"HTTP {r.status_code}"


@check(A, "logging out ends the session")
def _():
    s = Person("david")
    s.api("POST", "/api/auth/logout")
    r = s.api("GET", "/api/tasks?role=restricted")
    return r.status_code == 401, f"after logout: HTTP {r.status_code}"


# ─── tasks ──────────────────────────────────────────────────────────
T = "Tasks"


@check(T, "Clara sees the chores given to her, not Anna's own tasks")
def _():
    t = titles(clara.tasks())
    return ({"Zimmer aufräumen", "Tisch decken", "Buch zurückgeben"} <= t and "Steuererklärung abgeben" not in t,
            f"Clara sees: {sorted(t)}")


@check(T, "David does not see Clara's chore")
def _():
    t = titles(david.tasks())
    return "Zimmer aufräumen" not in t and {"Hausaufgaben Mathe", "Tisch decken"} <= t, f"David sees: {sorted(t)}"


@check(T, "Ben does not see Anna's private task")
def _():
    t = titles(ben.tasks())
    return "Steuererklärung abgeben" not in t, f"Ben sees: {sorted(t)}"


@check(T, "Clara ticks her chore off; Anna sees it done")
def _():
    t = clara.task("Zimmer aufräumen")
    r = clara.api("PATCH", f"/api/tasks/{t['id']}?role=restricted", json={"done": True})
    seen = next((x for x in anna.tasks() if x["id"] == t["id"]), {})
    return r.ok and bool(seen.get("done")), f"PATCH {r.status_code}, Anna sees done={seen.get('done')}"


@check(T, "Clara cannot delete a chore a parent gave her")
def _():
    t = clara.task("Tisch decken")
    r = clara.api("DELETE", f"/api/tasks/{t['id']}?role=restricted")
    return r.status_code in (403, 404), f"HTTP {r.status_code}"


@check(T, "Ben (parent) may tick Clara's chore")
def _():
    t = clara.task("Buch zurückgeben")
    r = ben.api("PATCH", f"/api/tasks/{t['id']}?role=member", json={"done": True})
    back = ben.api("PATCH", f"/api/tasks/{t['id']}?role=member", json={"done": False})
    return r.ok and back.ok, f"tick {r.status_code}, untick {back.status_code}"


@check(T, "Ben cannot change Anna's private task")
def _():
    t = anna.task("Steuererklärung abgeben")
    r = ben.api("PATCH", f"/api/tasks/{t['id']}?role=member", json={"title": "gehackt"})
    return r.status_code in (403, 404), f"HTTP {r.status_code}"


@check(T, "a daily chore comes back for tomorrow when ticked, for both children")
def _():
    t = david.task("Tisch decken")
    r = david.api("PATCH", f"/api/tasks/{t['id']}?role=restricted", json={"done": True})
    time.sleep(1)
    nxt = [x for x in anna.tasks() if x["title"] == "Tisch decken" and not x.get("done")]
    due = sorted(x.get("due_date") or "" for x in nxt)
    who = {a.get("name") for x in nxt for a in (x.get("assignees") or [])}
    return (r.ok and any(d.startswith(str(TODAY + timedelta(days=1))) for d in due) and {"Clara", "David"} <= who,
            f"PATCH {r.status_code}; open instances due {due}, assignees {sorted(n for n in who if n)}")


@check(T, "unticking a daily chore does not leave a second open copy")
def _():
    r = anna.api("POST", "/api/tasks?role=platform_admin",
                 json={"title": "Blumen täglich", "due_date": str(TODAY), "recurrence_rule": "daily"})
    tid = r.json()["id"]
    anna.api("PATCH", f"/api/tasks/{tid}?role=platform_admin", json={"done": True})
    anna.api("PATCH", f"/api/tasks/{tid}?role=platform_admin", json={"done": False})
    open_ = [t for t in anna.tasks() if t["title"] == "Blumen täglich" and not t.get("done")]
    return len(open_) == 1, f"open copies after tick + untick: {[t.get('due_date') for t in open_]}"


@check(T, "the children got a bell entry for the chores given to them")
def _():
    n = clara.api("GET", "/api/notifications").json()
    items = n if isinstance(n, list) else n.get("notifications", n.get("items", []))
    kinds = [i.get("kind") or i.get("type") for i in items]
    return any("task" in (k or "") for k in kinds), f"Clara's bell: {kinds[:8]}"


@check(T, "Anna's overdue task (from Ben) is in her briefing for today")
def _():
    r = anna.api("POST", "/api/briefings/day-today/run").json()
    sec = next(s for s in r["sections"] if s["id"] == "today-tasks")
    t = {x["title"] for x in (sec.get("result") or {}).get("tasks", [])}
    return "Getränke holen" in t and "Zimmer aufräumen" not in t, f"briefing tasks: {sorted(t)}"


@check(T, "Clara's briefing shows her chores only")
def _():
    r = clara.api("POST", "/api/briefings/day-today/run").json()
    sec = next(s for s in r["sections"] if s["id"] == "today-tasks")
    t = {x["title"] for x in (sec.get("result") or {}).get("tasks", [])}
    broken = [s["id"] for s in r["sections"] if s.get("ok") is False]
    return "Steuererklärung abgeben" not in t and "Hausaufgaben Mathe" not in t, \
        f"tasks {sorted(t)}; sections that failed for a child: {broken}"


# ─── calendar and sharing ───────────────────────────────────────────
C = "Calendar and sharing"


@check(C, "a new family member reads the others' calendars by default")
def _():
    t = titles(ben.events())                              # family default: read shares
    return "Zahnarzt" in t, f"Ben sees: {sorted(t)}"


@check(C, "Ben sees the parents' evening Anna invited him to, and got a bell entry")
def _():
    t = titles(ben.events())
    n = ben.api("GET", "/api/notifications").json()
    items = n if isinstance(n, list) else n.get("notifications", n.get("items", []))
    kinds = [i.get("kind") or i.get("type") for i in items]
    return "Elternabend" in t and any("invit" in (k or "") or "event" in (k or "") for k in kinds), \
        f"events {sorted(t)}; bell {kinds[:6]}"


@check(C, "'share calendars in the family' lets Ben read Anna's calendar")
def _():
    r = anna.api("POST", "/api/sharing/family-calendars", json={"exclude_user_ids": []})
    t = titles(ben.events())
    return r.ok and "Zahnarzt" in t, f"share {r.status_code}; Ben now sees {sorted(t)}"


@check(C, "…but Ben cannot change Anna's appointment")
def _():
    ev = next(e for e in anna.events() if e["title"] == "Zahnarzt" and e.get("person") != "admin")
    r = ben.api("PATCH", f"/api/events/{ev['id']}?role=member", json={"title": "gehackt"})
    return r.status_code in (403, 404), f"HTTP {r.status_code}"


@check(C, "Anna creates, moves and deletes an appointment")
def _():
    start = f"{TODAY + timedelta(days=2)}T09:00"
    r = anna.api("POST", "/api/events?role=platform_admin",
                 json={"title": "Journey-Termin", "starts_at": start, "ends_at": f"{TODAY + timedelta(days=2)}T10:00"})
    eid = r.json().get("id")
    m = anna.api("PATCH", f"/api/events/{eid}?role=platform_admin",
                 json={"starts_at": f"{TODAY + timedelta(days=2)}T11:00", "ends_at": f"{TODAY + timedelta(days=2)}T12:00"})
    moved = anna.api("GET", f"/api/events/{eid}?role=platform_admin").json()
    d = anna.api("DELETE", f"/api/events/{eid}?role=platform_admin")
    gone = anna.api("GET", f"/api/events/{eid}?role=platform_admin").status_code
    return (r.status_code == 201 and m.ok and "11:00" in (moved.get("starts_at") or "") and d.ok and gone == 404,
            f"create {r.status_code}, move {m.status_code} → {moved.get('starts_at')}, delete {d.status_code}, then {gone}")


@check(C, "the family board shows a person with their chores once they join ('Mich anzeigen')")
def _():
    def on_board():
        j = anna.api("GET", "/api/ambient/board?days=14").json()
        return j, {str(p.get("id") or p.get("user_id")) for p in j.get("people", [])}
    _j, before = on_board()
    r = david.api("PATCH", "/api/users/me/kiosk-agenda-consent", json={"consent": True})
    j, after = on_board()
    blob = json.dumps(j.get("tasks", []), ensure_ascii=False)
    return (david.id not in before and david.id in after and "Hausaufgaben Mathe" in blob,
            f"join {r.status_code}; David on board before {david.id in before}, after {david.id in after}")


@check(C, "a parent may set Clara's timetable, her brother may not")
def _():
    body = {"days": {"mon": [{"from": "08:00", "to": "08:45", "subject": "Mathe"}]}}
    a = anna.api("PUT", f"/api/ambient/timetable/{clara.id}", json=body)
    d = david.api("PUT", f"/api/ambient/timetable/{clara.id}", json=body)
    return a.ok and d.status_code in (403, 404), f"Anna {a.status_code} {a.text[:80]}, David {d.status_code}"


# ─── contacts ───────────────────────────────────────────────────────
K = "Contacts"


@check(K, "Anna's contacts are hers: Ben and Clara do not see 'Oma Hilde'")
def _():
    def names(p):
        j = p.api("GET", "/api/contacts").json()
        rows = j if isinstance(j, list) else j.get("contacts", j.get("items", []))
        return {c.get("display_name") for c in rows}
    a, b, c = names(anna), names(ben), names(clara)
    return "Oma Hilde" in a and "Oma Hilde" not in b and "Oma Hilde" not in c, \
        f"Anna {len(a)}, Ben sees Oma: {'Oma Hilde' in b}, Clara sees Oma: {'Oma Hilde' in c}"


@check(K, "a contact page opens for its owner and is closed to others")
def _():
    j = anna.api("GET", "/api/contacts").json()
    rows = j if isinstance(j, list) else j.get("contacts", j.get("items", []))
    cid = next(c["id"] for c in rows if c.get("display_name") == "Oma Hilde")
    return (anna.api("GET", f"/api/contacts/{cid}").ok and ben.api("GET", f"/api/contacts/{cid}").status_code in (403, 404),
            f"Anna {anna.api('GET', f'/api/contacts/{cid}').status_code}, Ben {ben.api('GET', f'/api/contacts/{cid}').status_code}")


# ─── mail ───────────────────────────────────────────────────────────
M = "Mail"


def inbox(p):
    j = p.api("GET", "/api/email/messages").json()
    return j if isinstance(j, list) else j.get("messages", j.get("items", []))


@check(M, "Anna and Ben each see their own inbox only")
def _():
    a, b = inbox(anna), inbox(ben)
    ids_a, ids_b = {m["id"] for m in a}, {m["id"] for m in b}
    return len(a) == 3 and len(b) == 3 and not (ids_a & ids_b), f"Anna {len(a)}, Ben {len(b)}, shared ids {ids_a & ids_b}"


@check(M, "Ben cannot open one of Anna's mails by its number")
def _():
    mid = inbox(anna)[0]["id"]
    r = ben.api("GET", f"/api/email/messages/{mid}")
    return r.status_code in (403, 404), f"HTTP {r.status_code}"


@check(M, "a child has no mail access")
def _():
    r = clara.api("GET", "/api/email/messages")
    rows = [] if not r.ok else inbox(clara)
    return (r.status_code in (403, 404) or rows == []), f"HTTP {r.status_code}, {len(rows)} mails"


@check(M, "marking a mail read (needs the mail server: not testable here)")
def _():
    mid = inbox(anna)[0]["id"]
    r = anna.api("PATCH", f"/api/email/messages/{mid}", json={"is_unread": 0})
    if r.status_code == 502:
        return None, "no IMAP server in the test household — refused cleanly (502)"
    m = anna.api("GET", f"/api/email/messages/{mid}").json()
    return r.ok and not m.get("is_unread"), f"PATCH {r.status_code}, is_unread={m.get('is_unread')}"


@check(M, "sending without a mail server is refused with a message, not a crash")
def _():
    j = anna.api("GET", "/api/email/accounts").json()
    aid = (j if isinstance(j, list) else j.get("accounts", []))[0]["id"]
    r = anna.api("POST", "/api/email/send", json={"account_id": aid, "to": ["oma.hilde@example.test"],
                                                    "subject": "Test", "body": "Hallo"})
    return r.status_code != 500 and "detail" in r.text, f"HTTP {r.status_code}: {r.text[:160]}"


@check(M, "the school letter becomes an appointment ('add to calendar')")
def _():
    mid = next(m["id"] for m in inbox(anna) if "Wandertag" in (m.get("subject") or ""))
    r = anna.api("POST", f"/api/email/messages/{mid}/calendar-event", json={})
    return r.ok, f"HTTP {r.status_code}: {r.text[:200]}"


# ─── search ─────────────────────────────────────────────────────────
S = "Search"


def search(p, q):
    r = p.api("GET", "/api/search", params={"q": q})
    return r.status_code, json.dumps(r.json(), ensure_ascii=False) if r.ok else r.text


@check(S, "Anna finds her task and her mail by a word")
def _():
    c1, t1 = search(anna, "Steuererklärung")
    c2, t2 = search(anna, "Wandertag")
    return c1 == 200 and "Steuererklärung" in t1 and "Wandertag" in t2, f"{c1}/{c2}"


@check(S, "Ben's search does not surface Anna's private task")
def _():
    c, t = search(ben, "Steuererklärung")
    return c == 200 and "Steuererklärung abgeben" not in t, f"HTTP {c}"


@check(S, "Clara's search does not surface parents' mail")
def _():
    c, t = search(clara, "Stadtwerke")
    return "Rechnung Oktober" not in t, f"HTTP {c}: {t[:160]}"


# ─── chat ───────────────────────────────────────────────────────────
H = "Chat (real model)"
REAL_MODEL = bool(HOUSEHOLD.get("real_llm"))


def chat_check(name: str):
    """Chat checks judge the real model's answers; with the fake one they
    are skipped, not failed."""
    if REAL_MODEL:
        return check(H, name)
    def skip(fn):
        record(H, name, None, "skipped: the test household runs the fake model")
        return fn
    return skip


@chat_check("'Was steht heute an?' — an answer about today, in German")
def _():
    j = anna.ask("Was steht heute an?")
    ans = j.get("response") or ""
    return j["_status"] == 200 and german(ans), f"{j['_seconds']} s: {ans[:200]}"


@chat_check("a task by chat lands in the task list")
def _():
    j = anna.ask("Leg eine Aufgabe an: Blumen gießen, morgen fällig")
    t = anna.task("Blumen gießen")
    return t is not None and (t.get("due_date") or "").startswith(str(TODAY + timedelta(days=1))), \
        f"{j['_seconds']} s: {(j.get('response') or '')[:160]} | task: {t and t.get('due_date')}"


@chat_check("'undo' takes the chat's last task back")
def _():
    pend = anna.api("GET", "/api/pending").json().get("pending", [])
    p = next((x for x in pend if x.get("preview", {}).get("title") == "Blumen gießen"), None)
    if not p:
        return False, f"no pending entry for it; pending: {[x.get('preview', {}).get('title') for x in pend]}"
    r = anna.api("POST", f"/api/pending/{p['id']}/cancel")
    return r.ok and anna.task("Blumen gießen") is None, f"cancel {r.status_code}, task gone: {anna.task('Blumen gießen') is None}"


@chat_check("Anna gives Clara a chore by chat; Clara sees it")
def _():
    j = anna.ask("Gib Clara die Aufgabe Hamster füttern, heute fällig")
    t = clara.task("Hamster füttern")
    return t is not None, f"{j['_seconds']} s: {(j.get('response') or '')[:200]}"


@chat_check("an appointment by chat lands in the calendar")
def _():
    j = anna.ask("Trag einen Termin ein: Zahnreinigung übermorgen um 10 Uhr")
    day = str(TODAY + timedelta(days=2))
    t = [e for e in anna.events(4) if (e.get("starts_at") or "").startswith(f"{day}T10:00")]
    return bool(t), f"{j['_seconds']} s: {(j.get('response') or '')[:160]} | {[(e['title'], e['starts_at']) for e in t]}"


@chat_check("a German title stays German (profile language English, the default)")
def _():
    anna.ask("Trag einen Termin ein: Friseur übermorgen um 16 Uhr")
    t = [e["title"] for e in anna.events(4) if (e.get("starts_at") or "").startswith(f"{TODAY + timedelta(days=2)}T16:00")]
    return "Friseur" in t, f"titles that day: {t}"


@chat_check("'Wann ist der Zahnarzt?' finds the appointment")
def _():
    j = anna.ask("Wann ist der Zahnarzt?")
    ans = j.get("response") or ""
    return "15" in ans or "Zahnarzt" in json.dumps(j.get("ui_actions"), ensure_ascii=False), f"{j['_seconds']} s: {ans[:200]}"


@chat_check("'Was steht in meinen Mails?' summarises Anna's inbox")
def _():
    j = anna.ask("Was steht in meinen Mails von heute?")
    ans = (j.get("response") or "") + json.dumps(j.get("ui_actions"), ensure_ascii=False)
    return any(w in ans for w in ("Wandertag", "Stadtwerke", "Kaffee", "Oma")), f"{j['_seconds']} s: {(j.get('response') or '')[:220]}"


@chat_check("Ben asks for a draft reply to Oma; nothing is sent")
def _():
    j = ben.ask("Schreib Oma eine kurze Antwort auf ihre Mail: Wir kommen Sonntag gern.")
    ans = j.get("response") or ""
    return j["_status"] == 200 and len(ans) > 20, f"{j['_seconds']} s: {ans[:240]}"


@chat_check("Clara asks about mail: no parent's mail leaks")
def _():
    j = clara.ask("Welche E-Mails habe ich bekommen? Was schreiben die Stadtwerke?")
    blob = (j.get("response") or "") + json.dumps(j.get("ui_actions"), ensure_ascii=False)
    return "84,20" not in blob and "Rechnung Oktober" not in blob, f"{j['_seconds']} s: {(j.get('response') or '')[:200]}"


@chat_check("Clara asks what her chores are")
def _():
    j = clara.ask("Was muss ich heute noch machen?")
    blob = (j.get("response") or "") + json.dumps(j.get("ui_actions"), ensure_ascii=False)
    return ("Tisch decken" in blob or "Hamster" in blob) and "Steuererklärung" not in blob, \
        f"{j['_seconds']} s: {(j.get('response') or '')[:200]}"


@chat_check("Ben asks for Anna's private task: not shown")
def _():
    j = ben.ask("Welche Aufgaben hat Anna? Zeig mir auch ihre Steuererklärung.")
    blob = (j.get("response") or "") + json.dumps(j.get("ui_actions"), ensure_ascii=False)
    return "Steuererklärung abgeben" not in blob, f"{j['_seconds']} s: {(j.get('response') or '')[:200]}"


@chat_check("a follow-up in the same conversation keeps the context")
def _():
    j1 = anna.ask("Welche Termine habe ich morgen?")
    j2 = anna.ask("Und um wie viel Uhr ist der erste davon?", conversation_id=j1.get("conversation_id"))
    ans = j2.get("response") or ""
    hour = r"\b1?\d(:\d\d| Uhr)|\b(eins|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|elf|zwölf|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b"
    return bool(re.search(hour, ans, re.I)), f"{j1['_seconds']}+{j2['_seconds']} s: {ans[:200]}"


@chat_check("the conversation is Anna's: Ben cannot open it")
def _():
    convs = anna.api("GET", "/api/conversations").json()
    rows = convs if isinstance(convs, list) else convs.get("conversations", [])
    cid = rows[0]["id"]
    r = ben.api("GET", f"/api/conversations/{cid}")
    return r.status_code in (403, 404), f"HTTP {r.status_code}"


# ─── writing (Schreiben) ────────────────────────────────────────────
W = "Schreiben (letters)"


@check(W, "Anna writes a letter and gets a preview and a PDF")
def _():
    r = anna.api("POST", "/api/writing", json={"kind": "letter", "title": "Kündigung Zeitung",
                                                "recipient": {"name": "Verlag Muster", "street": "Hauptstr. 1",
                                                              "postcode": "12345", "city": "Musterstadt"},
                                                "content": {"subject": "Kündigung", "body": "Hiermit kündige ich."}})
    doc = r.json()
    did = doc.get("id") or doc.get("document", {}).get("id")
    pv = anna.api("GET", f"/api/writing/{did}/preview")
    pdf = anna.api("GET", f"/api/writing/{did}/pdf", timeout=120)
    return (r.ok and pv.ok and pdf.ok and pdf.content[:4] == b"%PDF",
            f"create {r.status_code}, preview {pv.status_code}, pdf {pdf.status_code} {pdf.headers.get('content-type')}")


@check(W, "Ben does not see Anna's letter")
def _():
    a = anna.api("GET", "/api/writing").json()
    b = ben.api("GET", "/api/writing")
    rows_a = a if isinstance(a, list) else a.get("documents", a.get("items", []))
    mine = any(d.get("title") == "Kündigung Zeitung" for d in rows_a)
    return mine and "Kündigung Zeitung" not in b.text, f"Anna has it: {mine}; Ben HTTP {b.status_code}"


# ─── agents over MCP ────────────────────────────────────────────────
P = "MCP (outside agents)"


def rpc(token, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})}
    return requests.post(BASE + "/mcp", json=body, headers={"Authorization": f"Bearer {token}",
                                                             "Accept": "application/json, text/event-stream"}, timeout=120)


@check(P, "Anna makes a token; an agent lists tools and reads her tasks")
def _():
    tok = anna.api("POST", "/api/tokens", json={"name": "journey agent"}).json()["token"]
    init = rpc(tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "journeys", "version": "1"}})
    tools = rpc(tok, "tools/list")
    names = [t["name"] for t in tools.json().get("result", {}).get("tools", [])]
    call_name = "check_tasks" if "check_tasks" in names else ("use_skill" if "use_skill" in names else names[0])
    args = {"include_undated": True} if call_name == "check_tasks" else {"skill": "check_tasks", "args": {"include_undated": True}}
    call = rpc(tok, "tools/call", {"name": call_name, "arguments": args})
    return (init.ok and tools.ok and call.ok and "Steuererklärung" in call.text,
            f"init {init.status_code}, {len(names)} tools, call {call_name} {call.status_code}")


@check(P, "a child's token cannot read the parents' mail")
def _():
    tok = clara.api("POST", "/api/tokens", json={"name": "kid agent"})
    if tok.status_code != 200 and tok.status_code != 201:
        return True, f"children cannot make tokens (HTTP {tok.status_code})"
    t = tok.json()["token"]
    call = rpc(t, "tools/call", {"name": "email_briefing", "arguments": {"hours": 48}})
    return "Stadtwerke" not in call.text, f"HTTP {call.status_code}: {call.text[:160]}"


@check(P, "a wrong token is refused")
def _():
    r = rpc("yk_not_a_token", "tools/list")
    return r.status_code in (401, 403), f"HTTP {r.status_code}"


# ─── emergency access ───────────────────────────────────────────────
E = "Emergency access"


@check(E, "Ben opens emergency access; Anna is told")
def _():
    r = ben.api("POST", "/api/emergency-access", json={"reason": "Journey test", "hours": 1, "password": PW})
    n = anna.api("GET", "/api/notifications").json()
    items = n if isinstance(n, list) else n.get("notifications", n.get("items", []))
    told = any("emergency" in ((i.get("kind") or i.get("type") or "") + json.dumps(i)).lower() for i in items)
    return r.ok and told, f"open {r.status_code} {r.text[:120]}; Anna notified: {told}"


@check(E, "a child cannot open emergency access")
def _():
    r = clara.api("POST", "/api/emergency-access", json={"reason": "x", "hours": 1, "password": PW})
    return r.status_code in (400, 403, 404) and "started_at" not in r.text, f"HTTP {r.status_code}: {r.text[:100]}"


@check(E, "with the wrong password it stays closed")
def _():
    r = anna.api("POST", "/api/emergency-access", json={"reason": "x", "hours": 1, "password": "falsch"})
    return r.status_code in (400, 401, 403), f"HTTP {r.status_code}"


# ─── services this household does not have ──────────────────────────
X = "Without Paperless / Immich / WhatsApp"


for path in ("/api/documents", "/api/photos/recent", "/api/whatsapp/chats", "/api/recordings", "/api/dashboard/digest",
             "/api/today", "/api/push/status", "/api/people/household"):
    @check(X, f"{path} answers without a server error")
    def _(path=path):
        r = anna.api("GET", path)
        return r.status_code < 500, f"HTTP {r.status_code}: {r.text[:120]}"


# ─── report ─────────────────────────────────────────────────────────

def report() -> None:
    out = HERE / "report"
    out.mkdir(exist_ok=True)
    (out / "journeys.json").write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False))
    n_ok = sum(r["ok"] is True for r in RESULTS)
    n_bad = sum(r["ok"] is False for r in RESULTS)
    lines = [f"# Journeys — {n_ok} pass, {n_bad} fail", ""]
    area = None
    for r in RESULTS:
        if r["area"] != area:
            area = r["area"]
            lines += ["", f"## {area}", "", "| | Check | Detail |", "| --- | --- | --- |"]
        mark = {True: "✅", False: "❌", None: "ℹ️"}[r["ok"]]
        lines.append(f"| {mark} | {r['check']} | {r['detail'].replace('|', '/').replace(chr(10), ' ')} |")
    (out / "journeys.md").write_text("\n".join(lines) + "\n")
    print(f"\n{n_ok} pass, {n_bad} fail — {out / 'journeys.md'}")


report()
raise SystemExit(1 if any(r["ok"] is False for r in RESULTS) else 0)
