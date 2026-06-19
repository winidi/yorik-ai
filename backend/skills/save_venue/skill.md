---
name: save_venue
description: "Save a web-found venue as a business contact (website, address, optional cached prices)."
when_to_use: |
  After a successful web_extract on a venue page AND the user signalled they care about going there. Don't auto-save every web_extract.

  Signals to save:
    - User asked the price/hours AND will probably visit
    - User said "merk dir das", "speichern", "behalt dir das"
    - User asked you to compute a group price (so they ARE going)
    - User asked to add a calendar event with that location

  Don't save when:
    - User was just curious ("was kostet Cube E-Bike?" — no venue)
    - The page was an aggregator (schwimmbadcheck.de, yelp, etc.) — save
      the underlying venue's own page, not the directory.

  Saved venues become `contacts` rows with kind='business' — find_known_provider picks them up, future lookups skip the web.
inputs:
  display_name:
    type: string
    required: true
    description: Display name as you'd want it to appear in contacts. "Monkeytown Braunschweig", not just "Monkeytown".
  url:
    type: string
    required: false
    description: The venue's own website (NOT an aggregator). Used for "Website besuchen" + future structured-info refresh.
  category:
    type: string
    required: false
    description: |
      Free-text category that find_known_provider's category-keyword
      matcher recognises. Examples: "indoor-spielplatz", "schwimmbad",
      "restaurant", "zahnarzt", "werkstatt". Used to find the venue
      again next time the user mentions the type.
  address:
    type: string
    required: false
    description: Single-line postal address ("Straße + Nr., PLZ Ort"). Helps with travel-time calculation later.
  phone:
    type: string
    required: false
  email:
    type: string
    required: false
  notes:
    type: string
    required: false
    description: |
      Free-text remembered details (prices, hours, "kids' party 90 min",
      anything you want next time without re-fetching). Will be appended
      to the contact's notes column.
  price_table:
    type: object
    required: false
    description: |
      Optional structured prices captured during this lookup. Same
      shape as compute_group_price's items but persisted on the
      contact's notes for instant follow-up calcs. Example:
        [{"label": "Erwachsene", "unit_eur": 5.90}, ...]
outputs:
  contact_id:
    type: integer
permissions: [admin, member, restricted]
side_effects: |
  Inserts a row in `contacts` (kind='business'). Adds a contact_address
  + contact_channel entries if address/phone/email supplied. Emits a
  `venue_saved` ui_action so the chat can render a small "saved" card.
tags: [contacts, web, venue, memory]
---

# save_venue

The compound-knowledge primitive for web lookups. Every web_extract
about a place that the user cares about should land here, and the
NEXT lookup about that place should come from contacts — instant,
no LLM hallucination risk.
