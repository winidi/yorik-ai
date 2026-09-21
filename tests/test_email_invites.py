"""'Add to calendar' on an invitation mail: the ICS first, then the text."""

from datetime import date

from backend.email_invites import extract_appointment, parse_ics

GOOGLE_ICS = b"""BEGIN:VCALENDAR\r
PRODID:-//Google Inc//Google Calendar 70.9054//EN\r
METHOD:REQUEST\r
BEGIN:VEVENT\r
DTSTART:20260925T093000Z\r
DTEND:20260925T095000Z\r
SUMMARY:Dirk Winiecki and Alessandro Leto Barone\r
LOCATION:https://us05web.zoom.us/j/85130841877?pwd=abc\r
DESCRIPTION:Bring the deck\\, please.\\nSee you\r
  there.\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_google_invite_ics(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "Europe/Berlin"); time.tzset()
    ev = parse_ics(GOOGLE_ICS)
    assert ev["title"] == "Dirk Winiecki and Alessandro Leto Barone"
    assert ev["starts_at"] == "2026-09-25T11:30:00" and ev["ends_at"] == "2026-09-25T11:50:00"   # UTC → Berlin
    assert ev["all_day"] is False and ev["location"].startswith("https://us05web.zoom.us")
    assert ev["notes"] == "Bring the deck, please.\nSee you there." and ev["cancelled"] is False


def test_ics_with_tzid_and_all_day(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "Europe/Berlin"); time.tzset()
    ev = parse_ics("BEGIN:VEVENT\nDTSTART;TZID=America/New_York:20260925T090000\nDTEND;TZID=America/New_York:20260925T100000\nSUMMARY:Call\nEND:VEVENT")
    assert ev["starts_at"] == "2026-09-25T15:00:00"
    day = parse_ics("BEGIN:VEVENT\nDTSTART;VALUE=DATE:20261224\nDTEND;VALUE=DATE:20261227\nSUMMARY:Urlaub\nEND:VEVENT")
    assert day["all_day"] and day["starts_at"] == "2026-12-24T00:00:00" and day["ends_at"] == "2026-12-26T23:59:59"
    assert parse_ics(b"BEGIN:VCALENDAR\nEND:VCALENDAR") is None


def test_the_mail_that_failed():
    subject = ("Einladung von einem unbekannten Absender: Dirk Winiecki and Alessandro Leto Barone - "
               "Fr 25. Sep. 2026 11:30AM - 11:50AM (MESZ) (winidi89@gmail.com)")
    assert extract_appointment(subject) == {"date": "2026-09-25", "time": "11:30", "end_time": "11:50"}


def test_dates_and_times_in_prose():
    today = date(2026, 9, 21)
    assert extract_appointment("Termin am 02.10.2026 um 14 Uhr", today) == {"date": "2026-10-02", "time": "14:00"}
    assert extract_appointment("Wir sehen uns am 3. Oktober, 9:15 Uhr", today)["date"] == "2026-10-03"
    assert extract_appointment("Meeting on Oct 5, 2026 at 2:30 PM", today) == {"date": "2026-10-05", "time": "14:30"}
    assert extract_appointment("See you January 7th at 12:05 am", today) == {"date": "2027-01-07", "time": "00:05"}
    assert extract_appointment("2026-11-30 von 10:00 bis 11:30", today) == {"date": "2026-11-30", "time": "10:00", "end_time": "11:30"}
    assert extract_appointment("Rechnung 4711 über 84,00 EUR", today) == {}
    assert extract_appointment("am 31.02.2026", today) == {}                       # not a date
