---
name: find_known_provider
description: Find a provider the user already uses (contacts + invoices + events) before searching nearby.
when_to_use: |
  Whenever the user mentions a service provider category ("dentist",
  "GP", "garage", "hairdresser", "tax advisor") without naming
  one specifically. Yorik should check what the user already has on
  file before suggesting new ones.

  Examples:
    "dentist appointment on Tuesday" → find_known_provider(category="dentist")
    "I need to see the optician"     → find_known_provider(category="optician")
    "appointment at the hairdresser" → find_known_provider(category="hairdresser")

  Three places get checked, in order of trust:
    1. Contacts — anyone with relation matching ("my dentist", "Mrs
       Dr. Schmidt"), kind='business' + a relevant keyword in the name
       ("Dr. Miller dental practice"), or a category tag.
    2. Paperless — past invoices/letters with correspondents matching
       the category (e.g. the correspondent's name contains the word for "dentist" in the documents' language).
    3. Past calendar events — titles containing the category that have
       a location set.

  Outcomes:
    - 1 known → return them; LLM proceeds directly to add_calendar_event
    - >1 known → ask user which (relation? last-visited?) — do NOT silently pick the highest-trust source.
    - 0 known → fall through to find_provider_nearby(poi=…, near=…)
inputs:
  category:
    type: string
    required: true
    description: |
      Service category, in English (case-insensitive; German words are matched too):
      dentist, doctor, pharmacy,
      hospital, veterinary, optician,
      hairdresser, garage, lawyer,
      tax_advisor, accountant.
outputs:
  candidates:
    type: array
    description: |
      List of {source: "contact"|"paperless"|"calendar", contact_id?,
      name, address?, phone?, email?, last_seen?, evidence} — what we
      know about them and where the info came from.
  source_counts:
    type: object
    description: '{contacts: N, paperless: N, calendar: N} for quick triage.'
permissions: [admin, member, restricted]
side_effects: none — read only.
tags: [contacts, paperless, calendar, provider, search]
---

# find_known_provider

Defence-in-depth before reaching out to the open internet. The user's
own data has higher trust than a random Overpass result, and Yorik
should remember the dentist they invoiced last year.
