---
name: navigate_to
description: "Open / switch the browser to a Yorik app (calendar, chat, documents, etc.)."
when_not_to_use: |
  Reads ("how many tasks?", "wie viele / welche / was …") → call the matching `check_*` / `find_*` skill first and answer; navigate only after you've answered, never as a substitute for answering. Mutations ("add a task") → the matching domain skill. Help text — answer in chat. Most mutation skills already auto-navigate via `show_calendar` / `refresh_data` UI actions — only call this skill when nothing else is firing and the user explicitly asked to see something.
when_to_use: |
  Use this whenever the user wants to SEE or OPEN something, not do something with it. Trigger phrases (DE + EN):
  - "zeig mir X", "öffne X", "geh zu X", "ich will X sehen", "auf X"
  - "show me X", "open X", "go to X", "switch to X", "take me to X"

  Examples mapping intent → app:
  - "zeig mir die Kontakte" / "show me contacts" → app="contacts"
  - "öffne WhatsApp" / "open whatsapp"           → app="whatsapp"
  - "geh zum Kalender" / "go to calendar"        → app="calendar"
  - "ich will die Dokumente sehen" / "show docs" → app="documents"
  - "Brief schreiben" / "open compose"           → app="compose"
  - "Einstellungen" / "settings"                  → app="settings"
  - "zurück zur Startseite" / "go home"          → app="home"


inputs:
  app:
    type: string
    required: true
    description: |
      One of: home | calendar | chat | contacts | documents | compose
      | email | photos | tasks | whatsapp | briefing | settings.
      Names match the dock entries the user sees, so they're predictable.
  query_params:
    type: object
    required: false
    description: |
      Optional URL query parameters for deep-links. Examples:
        navigate_to(app="calendar", query_params={"date": "2026-06-15"})
        navigate_to(app="email",    query_params={"msg": 1234})
        navigate_to(app="contacts", query_params={"id": 257})
      Pass strings or numbers; everything is URL-encoded on the frontend.
outputs:
  navigated_to:
    type: string
    description: The resolved path (e.g. "/r/contacts?id=257") for logging.
permissions: [admin, member, restricted]
side_effects: |
  Emits a `navigate` ui_action that the chat / voice frontend handles by
  calling React Router's navigate(path). Pure navigation — no DB writes.
tags: [ui, navigation]
---

# navigate_to

The "show me / open" routing primitive. The chat reply that accompanies
this skill must be ONE short sentence in the user's language that names
the destination YOU JUST navigated to (the `app` argument). Don't copy
a fixed phrase — write a fresh acknowledgement that mentions the actual
screen ('Kalender' / 'calendar' / 'Kontakte' / 'contacts' / etc).

NEVER mention a different screen than the one you navigated to — a
mismatched reply ("Opening contacts" after navigate_to(app='calendar'))
makes the user instantly distrust the app.
