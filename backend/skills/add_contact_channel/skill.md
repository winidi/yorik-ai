---
name: add_contact_channel
description: "Attach a channel (email, phone, whatsapp, signal, sms, website, social) to a contact."
when_to_use: |
  - "Oma hat eine neue Handynummer …" → add_contact_channel(contact_id=N, kind='phone', value='…', label='mobile').
  - "speicher die Email von Müller GmbH: kontakt@..." → add_contact_channel(contact_id=N, kind='email', value='…').
  - When an unknown inbound channel arrives that the user wants linked to an existing contact instead of a new one.
  - If the (kind, value) is already attached to a DIFFERENT contact you'll get an error — that's a duplicate, fix the contact_id instead of forcing it through.
  - Always call find_person first to get the right contact_id.
inputs:
  contact_id:
    type: integer
    required: true
  kind:
    type: string
    required: true
    description: "One of 'email' | 'phone' | 'whatsapp' | 'signal' | 'telegram' | 'sms' | 'website' | 'social'."
  value:
    type: string
    required: true
    description: The channel value. Auto-normalised (emails lowercased, phones digit-stripped).
  label:
    type: string
    required: false
    description: "'primary' | 'work' | 'home' | 'mobile' | etc."
outputs:
  channel_id:
    type: integer
  contact:
    type: object
permissions: [admin, member, restricted]
side_effects: 1 row in contact_channels
tags: [contacts, mutation]
---

# add_contact_channel

Channels are unique on (kind, value) — the same email/phone can't be
attached to two different contacts. Trying to do so raises a clear
"already linked to contact X" error so the LLM can decide whether to
merge or pick the existing contact.
