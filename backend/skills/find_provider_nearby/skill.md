---
name: find_provider_nearby
description: "Find nearby businesses or POIs (dentist, pharmacy, etc.) via OpenStreetMap."
when_to_use: |
  When the user wants to find a place they don't already have:
    "Zahnarzt in Hannover" → poi='dentist', near='Hannover'
    "Apotheke um die Ecke" → poi='pharmacy', near=<user's home city>
    "Werkstatt für mein Auto" → poi='garage', near=<user's home city>

  **CALL find_known_provider FIRST.** Yorik already knows about the
  user's existing dentist/doctor/mechanic from their contacts, past
  invoices in Paperless, and past appointment history. Only fall back
  to find_provider_nearby when find_known_provider returned nothing.

  Common POI keywords (use these for `poi`, the connector knows the
  OSM tag mapping):
    Healthcare: dentist, doctor, hausarzt, pharmacy, hospital,
                veterinary, tierarzt
    Shops:      supermarket, bakery, hairdresser, optician
    Services:   bank, atm, post_office, garage, petrol_station
    Food:       restaurant, cafe, bar
  Unknown words fall back to a fuzzy name search — usually works but
  less precise than the tag-based search.

  After the user picks one, you can:
    - add_contact() to save the practice for next time
    - add_calendar_event() with location=picked_address to make an appointment slot
    - (later) book_appointment_via_email when that ships
inputs:
  poi:
    type: string
    required: true
    description: What kind of place? Use English or German keywords (see list above).
  near:
    type: string
    required: false
    description: City, neighborhood, or address to search around. Defaults to the user's home city when omitted.
  limit:
    type: integer
    required: false
    default: 12
    description: Max results (1-50). The chat usually shows 5-8.
outputs:
  pois:
    type: array
    description: List of {name, address, lat, lon, phone?, website?, opening_hours?, osm_id} sorted roughly by distance.
permissions: [admin, member, restricted]
side_effects: none — read-only Overpass query.
tags: [maps, search, poi, openstreetmap]
---

# find_provider_nearby

Overpass-backed business search. Free, no auth, uses OpenStreetMap data.
Pairs with `find_known_provider` (which checks the user's own data
first) so the chat shows "ich kenne Dr. Müller schon" before falling
back to "hier sind drei Zahnärzte in der Nähe von Hannover".
