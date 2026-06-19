"""Spoken-German time humanizer for the voice-TTS path.

The LLM emits times as digits ("18:00", "halb 19", "18:00–19:00 Uhr") which
Supertonic / any TTS engine reads stilted or wrong. This module rewrites
them into natural-spoken German *just before* synthesis — the on-screen
chat text stays unchanged.

Conventions used (matches the user's spec, decided 2026-05-25):

  06:00 → "sechs Uhr morgens"
  06:15 → "viertel nach sechs"
  06:30 → "halb sieben morgens"
  06:45 → "viertel vor sieben"
  12:00 → "zwölf Uhr mittags"
  14:30 → "halb drei nachmittags"
  18:00 → "sechs Uhr abends"
  18:30 → "halb sieben abends"
  21:00 → "neun Uhr abends"
  00:30 → "halb eins nachts"

Range collapse: when both endpoints share a tageszeit, render as
``"von <h1> bis <h2> Uhr <tageszeit>"`` so the audio doesn't repeat
the time-of-day phrase. Cross-tageszeit ranges (e.g. 17:00–19:00,
late-afternoon → evening) get both suffixes spelled out.

Non-quarter minute times fall through to ``"<H> Uhr <M>"`` with German
number words (so "13:42" becomes "ein Uhr zweiundvierzig nachmittags").

Pure function — no LLM, no I/O. Unit-testable in isolation.
"""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# German number words
# ---------------------------------------------------------------------------


# 1 is "ein" before "Uhr" / in compound (einundzwanzig). For standalone
# minutes (rare), the spoken form would be "eins" — we accept that gap.
_ONES = [
    "null", "ein", "zwei", "drei", "vier",
    "fünf", "sechs", "sieben", "acht", "neun",
]
_TEENS = [
    "zehn", "elf", "zwölf", "dreizehn", "vierzehn",
    "fünfzehn", "sechzehn", "siebzehn", "achtzehn", "neunzehn",
]
_TENS = {
    20: "zwanzig", 30: "dreißig", 40: "vierzig",
    50: "fünfzig", 60: "sechzig", 70: "siebzig",
    80: "achtzig", 90: "neunzig",
}


def _german_word(n: int) -> str:
    """0–99 → German word form.

    Hour 1 → "ein" (not "eins") since we always read "ein Uhr ...".
    Compound numbers like 21 use "ein": "einundzwanzig".
    """
    if n < 0 or n >= 100:
        return str(n)
    if n < 10:
        return _ONES[n]
    if n < 20:
        return _TEENS[n - 10]
    tens = (n // 10) * 10
    ones = n % 10
    if ones == 0:
        return _TENS[tens]
    return f"{_ONES[ones]}und{_TENS[tens]}"


# ---------------------------------------------------------------------------
# Tageszeit (time-of-day) suffix
# ---------------------------------------------------------------------------


def _tageszeit(h: int, m: int = 0) -> str:
    """Return "morgens" / "mittags" / "nachmittags" / "abends" / "nachts".

    Buckets (German conversational use):
      05:00–11:59 → morgens
      12:00–12:30 → mittags    (narrow window — only the lunch hour)
      12:31–17:59 → nachmittags
      18:00–21:59 → abends
      22:00–04:59 → nachts
    """
    if h == 12 and m <= 30:
        return "mittags"
    if 5 <= h < 12:
        return "morgens"
    if 12 < h < 18 or (h == 12 and m > 30):
        return "nachmittags"
    if 18 <= h < 22:
        return "abends"
    return "nachts"


# ---------------------------------------------------------------------------
# Time → natural German
# ---------------------------------------------------------------------------


def _hour12(h24: int) -> int:
    """24-hour → 12-hour. 0 → 12, 13 → 1, ..., 24 → 12."""
    h = h24 % 24
    return ((h - 1) % 12) + 1 if h != 0 else 12


def _hour_word(h24: int, *, standalone: bool = False) -> str:
    """German word for the 12-hour form of h24.

    German has two forms of "1":
      - "ein" before "Uhr" or in compounds ("einundzwanzig")
      - "eins" standalone ("halb eins" = 12:30/00:30)
    Pass ``standalone=True`` for the halb/viertel-vor next-hour position.
    """
    h12 = _hour12(h24)
    if h12 == 1 and standalone:
        return "eins"
    return _german_word(h12)


def humanize_single(h: int, m: int, *, omit_tageszeit: bool = False,
                    omit_uhr_for_full_hour: bool = False) -> str:
    """Render one HH:MM as natural German.

    ``omit_tageszeit``: skip the tageszeit suffix (for range endpoints).
    ``omit_uhr_for_full_hour``: skip the "Uhr" word on full-hour times
        (also for range endpoints when both endpoints are full hours —
        the range collapser adds a single trailing "Uhr <suffix>").
    """
    if not (0 <= h <= 24 and 0 <= m <= 59):
        return f"{h:02d}:{m:02d}"   # bail — caller can keep the original

    suffix = "" if omit_tageszeit else _tageszeit(h, m)

    if m == 0:
        uhr = "" if omit_uhr_for_full_hour else "Uhr"
        return _join(_hour_word(h), uhr, suffix)
    if m == 15:
        # "viertel nach sechs" — tageszeit on this hour
        return _join("viertel nach", _hour_word(h), suffix)
    if m == 30:
        # "halb sieben" — refers to the NEXT hour, standalone "eins" for 1
        next_h = (h + 1) % 24
        return _join("halb", _hour_word(next_h, standalone=True),
                     "" if omit_tageszeit else _tageszeit(next_h, 0))
    if m == 45:
        # "viertel vor sieben" — refers to the NEXT hour, standalone "eins"
        next_h = (h + 1) % 24
        return _join("viertel vor", _hour_word(next_h, standalone=True),
                     "" if omit_tageszeit else _tageszeit(next_h, 0))

    # Arbitrary minutes — "ein Uhr zweiundvierzig nachmittags".
    return _join(_hour_word(h), "Uhr", _german_word(m), suffix)


def _join(*parts: str) -> str:
    """Join non-empty parts with single spaces."""
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Range collapser — "18:00–19:00" → "von sechs bis sieben Uhr abends"
# ---------------------------------------------------------------------------


def humanize_range(h1: int, m1: int, h2: int, m2: int) -> str:
    """Render an HH:MM–HH:MM range.

    If both endpoints share the same tageszeit, collapse to a single
    trailing suffix. When both endpoints are also full-hours we additionally
    drop the inner "Uhr" so we get "von sechs bis sieben Uhr abends"
    instead of "von sechs Uhr bis sieben Uhr abends".

    Cross-tageszeit ranges spell out both suffixes ("von fünf Uhr
    nachmittags bis sieben Uhr abends").
    """
    t1, t2 = _tageszeit(h1, m1), _tageszeit(h2, m2)
    if t1 == t2:
        both_full_hour = (m1 == 0 and m2 == 0)
        left  = humanize_single(h1, m1, omit_tageszeit=True,
                                omit_uhr_for_full_hour=both_full_hour)
        right = humanize_single(h2, m2, omit_tageszeit=True,
                                omit_uhr_for_full_hour=both_full_hour)
        if both_full_hour:
            return f"von {left} bis {right} Uhr {t1}".rstrip()
        return f"von {left} bis {right} {t1}".rstrip()
    left  = humanize_single(h1, m1)
    right = humanize_single(h2, m2)
    return f"von {left} bis {right}"


# ---------------------------------------------------------------------------
# Top-level: rewrite a whole string
# ---------------------------------------------------------------------------


# Match HH:MM ranges first (dash, en-dash, em-dash, " bis "), then bare HH:MM.
# Bounded by \b on the outer edges to avoid mangling currency / versions.
_RANGE_RE = re.compile(
    r"\b(?P<h1>\d{1,2}):(?P<m1>\d{2})\s*"
    r"(?:[-‐-―−]|bis)\s*"  # ASCII -, all Unicode dashes, or "bis"
    r"(?P<h2>\d{1,2}):(?P<m2>\d{2})\b",
)
_SINGLE_RE = re.compile(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\b")


def humanize_times_de(text: str) -> str:
    """Replace every HH:MM and HH:MM–HH:MM in *text* with natural German.

    Order matters: ranges first (otherwise the singles regex eats the
    left endpoint and leaves the right one dangling).

    Also strips a redundant " Uhr" that the LLM commonly appends after
    the range ("18:00–19:00 Uhr" → "von sechs bis sieben Uhr abends"
    without a trailing "Uhr").
    """
    if not text:
        return text

    def _range_sub(m: re.Match) -> str:
        h1 = int(m.group("h1")); m1 = int(m.group("m1"))
        h2 = int(m.group("h2")); m2 = int(m.group("m2"))
        return humanize_range(h1, m1, h2, m2)

    out = _RANGE_RE.sub(_range_sub, text)

    def _single_sub(m: re.Match) -> str:
        h = int(m.group("h")); mm = int(m.group("m"))
        return humanize_single(h, mm)

    out = _SINGLE_RE.sub(_single_sub, out)

    # Tidy a few common follow-on tokens the LLM emits:
    # "<humanized> Uhr" → "<humanized>" when the humanized form already
    # contains "Uhr" or a quarter phrase. Just collapse double "Uhr".
    out = re.sub(r"(\bUhr)\s+Uhr\b", r"\1", out)
    # "<humanized>) Uhr" — same, with closing paren in between
    out = re.sub(r"(\b(?:morgens|mittags|nachmittags|abends|nachts))\s+Uhr\b",
                 r"\1", out)
    # "viertel nach sechs Uhr" — drop the trailing Uhr (it's redundant)
    out = re.sub(r"(viertel (?:nach|vor) \w+) Uhr\b", r"\1", out)
    # "halb sieben morgens Uhr" → already covered above
    return out


# ---------------------------------------------------------------------------
# Date → natural German
# ---------------------------------------------------------------------------
#
# The LLM emits dates as DD.MM.YYYY / DD.MM. / YYYY-MM-DD which TTS reads
# stilted ("null eins punkt null sechs punkt zweitausendsechsundzwanzig").
# When the date is in a useful window relative to "now" we collapse to
# "heute" / "morgen" / "am Montag" / "am Montag nächste Woche"; otherwise
# we render "ersten Juni zweitausendsechsundzwanzig" — full spoken German.

from datetime import date, datetime, timedelta

_MONTHS_DE_GEN = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]
_WEEKDAYS_DE = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
]
_ORDINAL_1_31 = {
    1:  "ersten",   2:  "zweiten",  3:  "dritten",   4:  "vierten",
    5:  "fünften",  6:  "sechsten", 7:  "siebten",   8:  "achten",
    9:  "neunten", 10: "zehnten",  11: "elften",    12: "zwölften",
    13: "dreizehnten",  14: "vierzehnten", 15: "fünfzehnten",
    16: "sechzehnten",  17: "siebzehnten",  18: "achtzehnten",
    19: "neunzehnten",  20: "zwanzigsten",  21: "einundzwanzigsten",
    22: "zweiundzwanzigsten", 23: "dreiundzwanzigsten",
    24: "vierundzwanzigsten", 25: "fünfundzwanzigsten",
    26: "sechsundzwanzigsten", 27: "siebenundzwanzigsten",
    28: "achtundzwanzigsten", 29: "neunundzwanzigsten",
    30: "dreißigsten", 31: "einunddreißigsten",
}


def _year_word(y: int) -> str:
    """Speak a four-digit year naturally. 2026 → "zweitausendsechsundzwanzig"."""
    if 2000 <= y <= 2099:
        rest = y - 2000
        if rest == 0:
            return "zweitausend"
        return "zweitausend" + _german_word(rest)
    # Fallback — let TTS chew the digits.
    return str(y)


def _spoken_date_de(d: date, *, with_year: bool, with_article: bool = True) -> str:
    """Render a date as natural German. "am ersten Juni" (article on) or
    "ersten Juni" (off, for cases where another preposition leads in)."""
    day_word = _ORDINAL_1_31.get(d.day, str(d.day))
    month = _MONTHS_DE_GEN[d.month - 1]
    parts = [day_word, month]
    if with_year:
        parts.append(_year_word(d.year))
    body = " ".join(parts)
    return f"am {body}" if with_article else body


def _relative_date_de(d: date, today: date, *, with_article: bool = True) -> str:
    """Return relative phrasing when the date is in a useful window
    around today, else None — caller falls back to _spoken_date_de.
    """
    delta = (d - today).days
    if delta == 0:    return "heute"
    if delta == 1:    return "morgen"
    if delta == 2:    return "übermorgen"
    if delta == -1:   return "gestern"
    if delta == -2:   return "vorgestern"
    wd = _WEEKDAYS_DE[d.weekday()]
    article = "am " if with_article else ""
    if 3 <= delta <= 7:
        return f"{article}{wd}"
    if 8 <= delta <= 14:
        return f"{article}{wd} nächste Woche"
    return ""  # outside window — caller uses spelled-out date


_DATE_PATTERNS = [
    # DD.MM.YYYY  ("01.06.2026")
    re.compile(r'(?<!\d)(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})(?!\d)'),
    # DD.MM.YY    ("01.06.26") — 2-digit year mapped to 2000+YY
    re.compile(r'(?<!\d)(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{2})(?!\d)'),
    # YYYY-MM-DD  ("2026-06-01") — ISO
    re.compile(r'(?<!\d)(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?!\d)'),
    # DD.MM.      ("am 01.06.") — no year, assume current year (or next if past)
    re.compile(r'(?<!\d)(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?!\d)'),
]


def humanize_dates_de(text: str, now: Optional[datetime] = None) -> str:
    """Replace numeric dates in *text* with spoken German.

    Behavior:
      - within ±2 days of today → "heute"/"morgen"/"übermorgen"/"gestern"/"vorgestern"
      - +3..+7 days  → "am Montag" / etc.
      - +8..+14 days → "am Montag nächste Woche"
      - else → "am ersten Juni zweitausendsechsundzwanzig" (year only if it
        differs from current OR the pattern explicitly carried a year)

    Idempotent: words like "morgen" / "heute" / "Montag" already there are
    left alone. We also try to strip a leading "am" the LLM already wrote
    before a numeric date so we don't double up ("am am ersten Juni").
    """
    if not text:
        return text
    today = (now or datetime.now()).date()

    # Prepositions the LLM commonly writes BEFORE a date. When present,
    # we keep the preposition AND skip our own "am " article so the
    # rendered phrase stays grammatical ("zum 01.06" → "zum Montag",
    # not "zum am Montag"; "am 28.05." → "morgen", not "am morgen").
    # Order matters in the alternation: longer first so "vom" beats "v".
    _PREP_RE = r'(?P<prep>\b(?:am|vom|zum|ab|bis|seit|nach|vor|um|gegen)\s+)?'

    def _swap_with_prep(m: re.Match, *, two_digit_year: bool) -> str:
        prep = (m.group("prep") or "").rstrip() + (" " if m.group("prep") else "")
        try:
            day = int(m.group("d"))
            month = int(m.group("m"))
            year_raw = m.group("y") if "y" in m.groupdict() and m.group("y") else None
            if year_raw is None:
                year = today.year
                tentative = date(year, month, day)
                if tentative < today:
                    year += 1
                d = date(year, month, day)
                source_had_year = False
            else:
                year = int(year_raw)
                if two_digit_year:
                    year = 2000 + year
                d = date(year, month, day)
                source_had_year = True
        except (ValueError, KeyError):
            return m.group(0)
        # When a preposition is present, drop the article ("am ") that
        # _relative_date_de / _spoken_date_de would otherwise prepend.
        has_prep = bool(m.group("prep"))
        rel = _relative_date_de(d, today, with_article=not has_prep)
        if rel:
            # "heute" / "morgen" / "übermorgen" are adverbs — they don't
            # take a preposition. Drop the LLM's "am / zum / vom" in
            # those cases too.
            if rel in {"heute", "morgen", "übermorgen", "gestern", "vorgestern"}:
                return rel
            return prep + rel if has_prep else rel
        spoken = _spoken_date_de(d,
                                  with_year=source_had_year or d.year != today.year,
                                  with_article=not has_prep)
        return prep + spoken if has_prep else spoken

    out = text
    for i, pat in enumerate(_DATE_PATTERNS):
        is_two_digit = (i == 1)
        # IGNORECASE so "Vom 30.05." / "Am 28.05." (sentence-start capitalisation)
        # also match the preposition. Date digits don't care about case.
        wrapped = re.compile(_PREP_RE + pat.pattern, pat.flags | re.IGNORECASE)
        out = wrapped.sub(lambda m: _swap_with_prep(m, two_digit_year=is_two_digit), out)
    return out


__all__ = [
    "humanize_times_de",
    "humanize_dates_de",
    "humanize_single",
    "humanize_range",
]
