"""The model writes exact values; the voice path turns them into speech
(2026-09-27: the prompt made the model say "halb neun" for 9:15 and
"halb eins" for 12:22, in the text chat too)."""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.agent.humanize import for_tts

NOW = datetime(2026, 9, 27, 12, 0)


@pytest.mark.parametrize("written, spoken", [
    ("Das Spiel ist um 9:15.", "Das Spiel ist um viertel nach neun morgens."),
    ("Erinnerung um 12:22 Uhr", "Erinnerung um zwölf Uhr zweiundzwanzig mittags"),
    ("von 15:00 bis 16:00", "von drei bis vier Uhr nachmittags"),            # no "von von"
    ("Sa, 10.10.", "am Samstag nächste Woche"),                              # weekday once
    ("um 9.15 Uhr", "um viertel nach neun morgens"),
    ("um 14 Uhr", "um zwei Uhr nachmittags"),
    ("Betrag 551,07 €", "Betrag fünfhunderteinundfünfzig Euro sieben"),
    ("€214.20 bezahlt", "zweihundertvierzehn Euro zwanzig bezahlt"),
    ("insgesamt 1.234,00 €", "insgesamt eintausendzweihundertvierunddreißig Euro"),
    ("IBAN DE32 5001 0517 5422 7163 31",
     "IBAN D E drei zwei, fünf null null eins, null fünf eins sieben, fünf vier zwei zwei, sieben eins sechs drei, drei eins"),
    ("Ich habe 3 Termine und 12 Mails.", "Ich habe 3 Termine und 12 Mails."),    # counts stay
    ("Version 2.10 ist da", "Version 2.10 ist da"),
])
def test_spoken_german(written, spoken):
    assert for_tts(written, "de", now=NOW) == spoken


def test_other_languages_pass_through():
    assert for_tts("The game is at 9:15, 551,07 €.", "en", now=NOW) == "The game is at 9:15, 551,07 €."


def test_time_of_day_is_the_times_own():
    # buckets as decided 2026-05-25: 22:00 on is "nachts"; 21:45 is still evening
    assert for_tts("um 21:45", "de", now=NOW) == "um viertel vor zehn abends"
    assert for_tts("um 22:45", "de", now=NOW) == "um viertel vor elf nachts"
    assert for_tts("um 0:30", "de", now=NOW) == "um halb eins nachts"
    assert for_tts("um 18:30", "de", now=NOW) == "um halb sieben abends"
