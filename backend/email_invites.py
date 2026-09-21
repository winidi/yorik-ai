"""Appointment data from an email: the invite's ICS when there is one,
otherwise dates and times read from the text.

An invitation (Google, Outlook, Apple) carries an `invite.ics` with
the exact start, end, title and place. That beats any guess from the
prose, so the "Add to calendar" button asks for it first.
"""

from __future__ import annotations

import re
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, Optional

# ─── ICS ─────────────────────────────────────────────────────────────


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\;", ";").replace("\\\\", "\\").strip()


def _ics_time(value: str, params: Dict[str, str]) -> tuple[Optional[datetime], bool]:
    """(local naive datetime, all_day). Yorik stores local wall time."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d"), True
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value)
    if not m:
        return None, False
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    tz = None
    if m.group(3):
        from datetime import timezone
        tz = timezone.utc
    elif params.get("TZID"):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(params["TZID"])
        except Exception:  # noqa: BLE001 — an unknown TZID reads as local time
            tz = None
    if tz is not None:
        dt = dt.replace(tzinfo=tz).astimezone().replace(tzinfo=None)
    return dt, False


def parse_ics(data: bytes | str) -> Optional[Dict[str, Any]]:
    """The first VEVENT of an ICS as {title, starts_at, ends_at, all_day,
    location, notes}; None when there is no usable event."""
    text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else data
    text = re.sub(r"\r?\n[ \t]", "", text)                      # unfold
    m = re.search(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S)
    if not m:
        return None
    props: Dict[str, tuple[str, Dict[str, str]]] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *rest = head.split(";")
        params = dict(p.split("=", 1) for p in rest if "=" in p)
        props.setdefault(name.upper(), (value, {k.upper(): v.strip('"') for k, v in params.items()}))
    if "DTSTART" not in props:
        return None
    start, all_day = _ics_time(*props["DTSTART"])
    if not start:
        return None
    end = None
    if "DTEND" in props:
        end, _ = _ics_time(*props["DTEND"])
    if all_day:
        # DTEND of an all-day event is exclusive
        last = (end - timedelta(days=1)) if end and end > start else start
        starts_at, ends_at = f"{start:%Y-%m-%d}T00:00:00", f"{last:%Y-%m-%d}T23:59:59"
    else:
        starts_at = start.isoformat(timespec="seconds")
        ends_at = (end or start + timedelta(hours=1)).isoformat(timespec="seconds")
    return {
        "title": _unescape(props.get("SUMMARY", ("", {}))[0]) or None,
        "starts_at": starts_at, "ends_at": ends_at, "all_day": all_day,
        "location": _unescape(props.get("LOCATION", ("", {}))[0]) or None,
        "notes": _unescape(props.get("DESCRIPTION", ("", {}))[0])[:1500] or None,
        "cancelled": "METHOD:CANCEL" in text.upper(),
    }


# ─── prose ───────────────────────────────────────────────────────────

_MONTHS = {
    "jan": 1, "januar": 1, "january": 1, "feb": 2, "februar": 2, "february": 2,
    "mär": 3, "maer": 3, "mrz": 3, "märz": 3, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "mai": 5, "may": 5, "jun": 6, "juni": 6, "june": 6, "jul": 7, "juli": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "okt": 10, "oct": 10, "oktober": 10, "october": 10,
    "nov": 11, "november": 11, "dez": 12, "dec": 12, "dezember": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})?(?!\d)")
_DATE_DMY_NAME_RE = re.compile(rf"\b(\d{{1,2}})\.?\s+({_MONTH_ALT})\.?,?\s*(\d{{4}})?", re.I)      # 25. Sep. 2026
_DATE_MDY_NAME_RE = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})?", re.I)  # Sep 25, 2026
_TIME_RE = re.compile(r"(?<![\d.:])(\d{1,2})[:.](\d{2})\s*(uhr|am|pm|a\.m\.|p\.m\.)?(?![\d.])", re.I)
_TIME_HOUR_RE = re.compile(r"(?<![\d.:])(\d{1,2})\s*(uhr|am|pm)\b", re.I)


def _valid(y: int, mo: int, d: int) -> Optional[str]:
    try:
        return _date(y, mo, d).isoformat()
    except ValueError:
        return None


def _hhmm(h: int, mi: int, suffix: Optional[str]) -> Optional[str]:
    s = (suffix or "").lower().replace(".", "")
    if s == "pm" and h < 12:
        h += 12
    if s == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if 0 <= h < 24 and 0 <= mi < 60 else None


def extract_appointment(text: str, today: Optional[_date] = None) -> Dict[str, str]:
    """Best-effort {date, time, end_time} from an email's text. Numeric
    and ISO dates, month names in German and English, 24-hour and
    AM/PM times, and the end of a "11:30 – 11:50" range."""
    today = today or _date.today()
    out: Dict[str, str] = {}
    text = text or ""

    found: Optional[tuple[int, str]] = None        # earliest date mention wins

    def consider(pos: int, iso: Optional[str]) -> None:
        nonlocal found
        if iso and (found is None or pos < found[0]):
            found = (pos, iso)

    def year_for(y: Optional[str], mo: int, d: int) -> int:
        if y:
            return int(y) + (2000 if len(y) == 2 else 0)
        guess = _valid(today.year, mo, d)
        return today.year + 1 if guess and guess < today.isoformat() else today.year

    if (m := _DATE_ISO_RE.search(text)):
        consider(m.start(), _valid(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    if (m := _DATE_DMY_NAME_RE.search(text)):
        mo = _MONTHS[m.group(2).lower()]
        consider(m.start(), _valid(year_for(m.group(3), mo, int(m.group(1))), mo, int(m.group(1))))
    if (m := _DATE_MDY_NAME_RE.search(text)):
        mo = _MONTHS[m.group(1).lower()]
        consider(m.start(), _valid(year_for(m.group(3), mo, int(m.group(2))), mo, int(m.group(2))))
    if (m := _DATE_NUM_RE.search(text)):
        mo, d = int(m.group(2)), int(m.group(1))
        if 1 <= mo <= 12:
            consider(m.start(), _valid(year_for(m.group(3), mo, d), mo, d))
    if found:
        out["date"] = found[1]

    times = [t for t in (_hhmm(int(m.group(1)), int(m.group(2)), m.group(3)) for m in _TIME_RE.finditer(text)) if t]
    if not times and (m := _TIME_HOUR_RE.search(text)):
        t = _hhmm(int(m.group(1)), 0, m.group(2))
        times = [t] if t else []
    if times:
        out["time"] = times[0]
        if len(times) > 1 and times[1] > times[0]:
            out["end_time"] = times[1]
    return out
