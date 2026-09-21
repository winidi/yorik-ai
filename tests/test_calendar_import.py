"""Import an .ics file; subscribe to a secret iCal address as a
read-only mirror. One direction only."""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.conftest import login_client

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:single@google.com
DTSTART:20260925T093000Z
DTEND:20260925T095000Z
SUMMARY:Call mit Alessandro
LOCATION:Zoom
END:VEVENT
BEGIN:VEVENT
UID:weekly@google.com
DTSTART;TZID=Europe/Berlin:20260901T180000
DTEND;TZID=Europe/Berlin:20260901T193000
RRULE:FREQ=WEEKLY;BYDAY=TU
SUMMARY:Kickboxen
END:VEVENT
BEGIN:VEVENT
UID:ending@google.com
DTSTART;TZID=Europe/Berlin:20260907T090000
DTEND;TZID=Europe/Berlin:20260907T093000
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=Europe/Berlin:20260914T090000
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:ending@google.com
RECURRENCE-ID;TZID=Europe/Berlin:20260921T090000
DTSTART;TZID=Europe/Berlin:20260921T110000
DTEND;TZID=Europe/Berlin:20260921T113000
SUMMARY:Standup (verschoben)
END:VEVENT
BEGIN:VEVENT
UID:gone@google.com
DTSTART:20260926T100000Z
STATUS:CANCELLED
SUMMARY:Abgesagt
END:VEVENT
BEGIN:VEVENT
UID:holiday@google.com
DTSTART;VALUE=DATE:20261224
DTEND;VALUE=DATE:20261227
SUMMARY:Weihnachten
END:VEVENT
END:VCALENDAR
"""


def test_parse_calendar_series_exceptions_and_all_day(monkeypatch):
    import time
    from backend.email_invites import parse_calendar
    monkeypatch.setenv("TZ", "Europe/Berlin"); time.tzset()
    rows = {r["ical_uid"]: r for r in parse_calendar(ICS, window_start=datetime(2026, 1, 1), window_end=datetime(2027, 12, 31))}
    assert rows["single@google.com"]["starts_at"] == "2026-09-25T11:30:00"
    assert rows["weekly@google.com"]["recurring"] == "weekly"                       # stays one series
    standups = sorted(r["starts_at"] for u, r in rows.items() if u.startswith("ending@"))
    assert standups == ["2026-09-07T09:00:00", "2026-09-21T11:00:00", "2026-09-28T09:00:00"]   # 14th skipped, 21st moved
    assert rows["ending@google.com#2026-09-21T09:00:00"]["title"] == "Standup (verschoben)"
    assert "gone@google.com" not in rows
    assert rows["holiday@google.com"]["all_day"] and rows["holiday@google.com"]["ends_at"] == "2026-12-26T23:59:59"


@pytest.fixture
def dirk(fresh_app):
    from backend import spaces as S
    from backend.calendars import ensure_calendars_for_user
    c, uid = login_client(fresh_app, role="platform_admin", name="Dirk", email="d@example.local")
    S.ensure_workspace_exists(uid, "Dirk"); S.ensure_personal_space(uid, "Dirk"); ensure_calendars_for_user(uid, "Dirk")
    cal = next(x for x in c.get("/api/calendars").json() if x["kind"] == "personal")
    return c, uid, cal["id"]


def _titles(client):
    r = client.get("/api/events?start=2026-09-01&end=2026-10-31")
    data = r.json()
    return sorted({e["title"] for e in (data.get("events") if isinstance(data, dict) else data)})


def test_import_file_twice_does_not_double(dirk):
    c, _, cal = dirk
    preview = c.post(f"/api/calendar-import/file?calendar_id={cal}&dry_run=1", files={"file": ("g.ics", ICS, "text/calendar")}).json()
    assert preview["dry_run"] and preview["events"] == 6 and preview["series"] == 1
    assert _titles(c) == []
    first = c.post(f"/api/calendar-import/file?calendar_id={cal}", files={"file": ("g.ics", ICS, "text/calendar")}).json()
    assert first["added"] == 6
    again = c.post(f"/api/calendar-import/file?calendar_id={cal}", files={"file": ("g.ics", ICS, "text/calendar")}).json()
    assert again["added"] == 0 and again["unchanged"] == 6
    assert "Kickboxen" in _titles(c) and "Standup (verschoben)" in _titles(c)
    assert c.post(f"/api/calendar-import/file?calendar_id={cal}", files={"file": ("x.ics", "hello", "text/plain")}).status_code == 400


def test_subscription_is_a_read_only_mirror(dirk, monkeypatch):
    from backend import calendar_import as CI
    c, _, _ = dirk
    source = {"body": ICS.encode()}
    monkeypatch.setattr(CI, "fetch_ics", lambda url, etag=None: (source["body"], "v1"))
    monkeypatch.setattr(CI, "_check_url", lambda url: url)

    feed = c.post("/api/calendar-import/feeds", json={"url": "https://calendar.google.com/x/basic.ics", "name": "Google"}).json()
    assert feed["sync"]["added"] == 6 and feed["url_host"] == "calendar.google.com"
    mirror = next(x for x in c.get("/api/calendars").json() if x["id"] == feed["calendar_id"])
    assert mirror["access_level"] == "read" and mirror["read_only"] == 1
    assert "url" not in feed and "url_enc" not in feed                       # the address is a secret

    # nobody edits a mirror here, the operator included
    ev = next(e for e in (c.get("/api/events?start=2026-09-01&end=2026-10-31").json()) if e["title"] == "Call mit Alessandro")
    assert c.patch(f"/api/events/{ev['id']}?role=platform_admin", json={"title": "x"}).status_code == 403
    assert c.post("/api/events?role=platform_admin", json={"title": "neu", "starts_at": "2026-09-30T10:00:00",
                  "calendar_id": feed["calendar_id"]}).status_code == 403

    # the source changes: one event renamed, one gone
    source["body"] = ICS.replace("Call mit Alessandro", "Call mit Alessandro (neu)").replace("SUMMARY:Weihnachten", "STATUS:CANCELLED\nSUMMARY:Weihnachten").encode()
    r = c.post(f"/api/calendar-import/feeds/{feed['id']}/sync").json()
    assert r["updated"] == 1 and r["removed"] == 1
    # a broken fetch changes nothing
    source["body"] = b"BEGIN:VCALENDAR\nEND:VCALENDAR"
    assert c.post(f"/api/calendar-import/feeds/{feed['id']}/sync").json()["ok"] is False
    assert "Call mit Alessandro (neu)" in _titles(c)
    assert c.get("/api/calendar-import/feeds").json()[0]["last_status"] == "error"

    assert c.delete(f"/api/calendar-import/feeds/{feed['id']}").status_code == 204
    assert _titles(c) == [] and all(x["id"] != feed["calendar_id"] for x in c.get("/api/calendars").json())


def test_a_wrong_address_leaves_nothing_behind(dirk, monkeypatch):
    from backend import calendar_import as CI
    c, _, _ = dirk
    before = len(c.get("/api/calendars").json())
    assert c.post("/api/calendar-import/feeds", json={"url": "http://calendar.google.com/x.ics", "name": "G"}).status_code == 400
    assert c.post("/api/calendar-import/feeds", json={"url": "https://192.168.1.5/x.ics", "name": "G"}).status_code == 400
    monkeypatch.setattr(CI, "_check_url", lambda url: url)
    monkeypatch.setattr(CI, "fetch_ics", lambda url, etag=None: (b"<html>login</html>", None))
    assert c.post("/api/calendar-import/feeds", json={"url": "https://example.org/x.ics", "name": "G"}).status_code == 400
    assert len(c.get("/api/calendars").json()) == before and c.get("/api/calendar-import/feeds").json() == []
