---
name: add_calendar_event
description: Create a new calendar event in Yorik's local calendar
when_not_to_use: |
  Anfahrt / Rückfahrt / drive-time blocks → use `block_travel_time` instead. This skill produces an unlinked plain event that misses [LINKED_TO=...] and won't cascade-delete with the main event.
when_to_use: |
  Trigger: "trag X ein", "block out", "schedule X", "add X to the calendar". Acknowledge AFTER the skill returns, never before. Reply ONE short line ("Termin eingetragen").

  If date or hour is missing, ask before calling — never default to 10:00 or pick a day yourself.

  DATE-ANCHOR DISCIPLINE: when the user named a relative date (weekday name, "morgen", "in 3 Tagen"), FIRST write the resolved date + weekday inline as a one-liner BEFORE calling the skill — e.g. "User said Friday → looking up table → 2026-06-05 (Freitag)". This catches the off-by-one weekday bug that hit T4. The lookup table in the NOW section is authoritative; do not compute day offsets yourself.

  Then after the skill returns, check that the echoed `verified_weekday` matches. If it doesn't, call update_calendar_event with the correct date BEFORE telling the user the event was created.

  For named household members: `find_person(query=..., source='household')` first; pass the returned id via `attendee_user_ids`. Free-text `person=` only for non-Yorik people (kids, guests). A `contact_id` from `source='contacts'` is NOT a valid attendee_user_id.

  Pass `location` whenever the user mentions WHERE — the backend geocodes it and the UI renders a travel-time badge ("leave at HH:MM"). Without it, the badge doesn't show.

  Provider categories (Zahnarzt, Hausarzt, Werkstatt, Friseur, Apotheke, Tierarzt, Steuerberater, Anwalt, Optiker): call `find_known_provider(category=...)` first; if 0 matches, `find_provider_nearby(poi=..., near=...)` next. Never skip to web search for these.

  If the user names their own provider not in the picker: `find_person`, then `add_contact` if missing, then this skill.

  If the returned event has `travel_time_s > 0`, offer `block_travel_time(event_id=<returned>)` — never auto-block. Skip the offer when travel_time_s is 0/null.

  Examples:
    - User accepts a meeting in WhatsApp ("yes, Thursday 2pm works") → add it.
    - "block out Friday 10–12 for dentist" → add it; find_known_provider for location.
    - Another skill records a commitment the user just made.
inputs:
  title:
    type: string
    required: true
    description: Short event title (e.g. "Dentist appointment", "Coffee with Anna")
  starts_at:
    type: string
    required: true
    description: ISO 8601 start (e.g. "2026-05-22T14:00:00"). Local time, no timezone suffix needed.
  ends_at:
    type: string
    required: false
    description: ISO 8601 end. Defaults to starts_at + 60 minutes.
  all_day:
    type: boolean
    required: false
    default: false
  person:
    type: string
    required: false
    description: Who it's for/with — "admin" | "member" | "child" | "all" | a name string
  notes:
    type: string
    required: false
  calendar_id:
    type: integer
    required: false
    description: |
      Target calendar. Omit to default to the creator's personal calendar
      — multi-attendee events still appear on every invited attendee's
      view via the attendee/RSVP join, so they don't need to live on the
      Household calendar. Pass an explicit calendar_id only when the user
      named one ("trag das in den Haushaltskalender ein") or when the item
      is genuinely person-agnostic (trash day, mortgage, holidays).
  attendee_user_ids:
    type: array
    required: false
    description: |
      User IDs to invite. Each gets a notification + RSVP buttons on the
      event. Use find_person / SELECT FROM user_profiles to resolve names
      to IDs first. NEVER guess an id.
  attendee_names:
    type: array
    required: false
    description: |
      Free-text attendee names for people without Yorik logins (kids,
      external guests). They appear on the event chip but no RSVP is
      requested. Prefer attendee_user_ids when the person has an account.
  visibility:
    type: string
    required: false
    default: default
    description: |
      'default' (uses calendar ACL) or 'private' (even on shared calendars,
      hide title+notes from anyone except the owner — they see 'Busy').
  location:
    type: string
    required: false
    description: |
      Where the appointment is — free-text address, business name +
      address, or city. Examples:
        - "Zahnarztpraxis Dr. Müller, Hauptstr. 7, 30159 Hannover"
        - "TU Berlin"
        - "Café Goldmund, Kreuzberg"
      When set, Yorik geocodes it and computes driving time from the
      user's home address (cached on the event row, shown as a travel-
      time badge in the calendar). Leave empty for at-home events.

      Pass this whenever the appointment is physically somewhere — even
      if the user didn't explicitly say "create event at X", infer from
      context: "Zahnarzttermin" → location = the dentist's address (call
      find_known_provider first to resolve "der Zahnarzt"); "treffen mit
      Anna im Café Goldmund" → location = "Café Goldmund".
  category:
    type: string
    required: false
    description: |
      Closed enum — drives the event's colour on the calendar. Pick the
      single best match from the user's phrasing; omit if genuinely
      ambiguous (no colour > wrong colour).
        - "family"   — Familie, Haushalt, Kinder, Eltern, Geburtstage
        - "business" — Arbeit, Meetings, Kundentermine, Liefertermine
        - "drive"    — Anfahrt, Rückfahrt, Reisezeit (you usually don't
                       set this manually — block_travel_time does it)
        - "health"   — Arzt, Zahnarzt, Therapie, Untersuchung, Apotheke
        - "personal" — Sport, Gym, Hobby, „Zeit für mich"
        - "social"   — Freunde, Essen, Feiern, Party
      Examples:
        "Zahnarzttermin"            → category="health"
        "Kundentermin SAP"          → category="business"
        "Schule abholen"            → category="family"
        "Sport im Studio"           → category="personal"
        "Essen mit Tom"             → category="social"
outputs:
  event_id:
    type: integer
    description: The newly-created event's id
  event:
    type: object
    description: The full inserted row {id, title, starts_at, ends_at, ...}
cost: 1 SQLite INSERT (no LLM, no network)
permissions: [admin, member, restricted]
side_effects: |
  Inserts a row into the events table. The calendar app re-renders on
  next refresh. Other skills that read the calendar (check_calendar,
  the WhatsApp draft generator) will see the new event immediately.
tags: [calendar, scheduling, write]
---

# add_calendar_event

Thin wrapper around the existing /api/events POST path. Validates the
ISO timestamp, defaults ends_at to starts_at+1h if missing, returns
the full inserted row so a downstream skill (e.g. a chat response)
can confirm specifics back to the user.
