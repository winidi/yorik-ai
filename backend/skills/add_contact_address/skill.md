---
name: add_contact_address
description: "Attach a postal address (home, work, billing, shipping) to an existing contact."
when_to_use: |
  - User dictates a new address for an existing contact.
  - YOU asked the user for grandma's address (after find_person returned no address), they gave it — call this to persist it.
  - For business contacts, billing address typically differs from shipping — use kind='billing' or 'shipping' explicitly.
  - Always call find_person first to get the right contact_id.
inputs:
  contact_id:
    type: integer
    required: true
  kind:
    type: string
    required: false
    default: home
    description: "'home' | 'work' | 'billing' | 'shipping' | 'other'."
  line1:
    type: string
    required: false
    description: Street + house number. At least one of line1/line2/postcode/city must be provided.
  line2:
    type: string
    required: false
  postcode:
    type: string
    required: false
  city:
    type: string
    required: false
  region:
    type: string
    required: false
    description: State / canton / Bundesland.
  country:
    type: string
    required: false
    description: "ISO 3166-1 alpha-2 ('DE', 'CH', 'US', …) preferred."
  label:
    type: string
    required: false
outputs:
  address_id:
    type: integer
  contact:
    type: object
permissions: [admin, member, restricted]
side_effects: 1 row in contact_addresses
tags: [contacts, mutation]
---

# add_contact_address

Multiple addresses per contact are normal — Oma's home, the office she
works at, a billing address for a business. Use the `kind` field so
Compose knows which one to render for invoices vs. letters.
