"""Event category enum — single source of truth.

The LLM picks one of these via add_calendar_event / update_calendar_event;
the frontend maps category → colour palette in
`frontend-react/src/apps/calendar/categoryPalette.ts`. Adding a new
category means updating both sides (and ideally adding a UI swatch).

We deliberately keep this list SHORT (≤8). Past that, colour-coding
stops helping the brain distinguish at a glance.
"""

from __future__ import annotations

# Canonical category slugs. Order = sort order in any picker UI.
EVENT_CATEGORIES: tuple[str, ...] = (
    "family",     # household / kids / parents (emerald)
    "business",   # work meetings, client calls (slate)
    "drive",      # Anfahrt / Rückfahrt buffers (amber)
    "health",     # doctor, therapy, dentist (rose)
    "personal",   # gym, hobbies, own time (violet)
    "social",     # friends, dinners, parties (sky)
)

# German labels for skill docs + LLM hints. The LLM sees these so it can
# match user phrasing ("Arzttermin" → health) reliably.
CATEGORY_LABELS_DE: dict[str, str] = {
    "family":   "Familie / Haushalt / Kinder / Eltern",
    "business": "Arbeit / Meetings / Kundentermine",
    "drive":    "Anfahrt / Rückfahrt / Reisezeit",
    "health":   "Arzt / Therapie / Zahnarzt / Gesundheit",
    "personal": "Sport / Hobby / persönliche Zeit",
    "social":   "Freunde / Essen / Feiern",
}


def normalize_category(value: str | None) -> str | None:
    """Return the canonical category slug for `value`, or None.

    Accepts the slug itself ('drive') or any reasonable user phrasing.
    Unknown values return None so the skill can reject cleanly instead
    of writing junk into the DB.
    """
    if not value:
        return None
    v = value.strip().lower()
    if v in EVENT_CATEGORIES:
        return v
    # A handful of common German/English synonyms — keeps the LLM from
    # having to learn the exact slug every time.
    synonyms = {
        "familie":    "family",
        "haushalt":   "family",
        "kinder":     "family",
        "kids":       "family",
        "work":       "business",
        "arbeit":     "business",
        "meeting":    "business",
        "geschäft":   "business",
        "geschaeft":  "business",
        "anfahrt":    "drive",
        "rückfahrt":  "drive",
        "rueckfahrt": "drive",
        "fahrt":      "drive",
        "travel":     "drive",
        "transit":    "drive",
        "arzt":       "health",
        "doctor":     "health",
        "zahnarzt":   "health",
        "therapie":   "health",
        "gesundheit": "health",
        "sport":      "personal",
        "gym":        "personal",
        "hobby":      "personal",
        "freunde":    "social",
        "friends":    "social",
        "essen":      "social",
        "party":      "social",
    }
    return synonyms.get(v)
