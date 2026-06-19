---
title: Contacts — import, edit, share
nav_app: contacts
summary: Manage your contact hub. Import .vcf, auto-capture from WhatsApp / email, share with household members, attach addresses + channels.
---

# Contacts — import, edit, share

Yorik's contacts hub is the single source of truth for "who" — persons, businesses, family members. Calendar attendees, letter recipients, invoice customers all pull from here.

## Adding contacts

Three paths:

- **Manual**: Contacts app → **Add contact** button. Fill name, kind (person / business), optional address + channels.
- **Import .vcf**: Drop a `.vcf` (or .vcard) file anywhere in chat or on the Contacts page. The vCard import modal opens. Pre-seeded with the parsed fields; you confirm + save.
- **Auto-capture**: when an email or WhatsApp message arrives from someone not in your hub, Yorik suggests adding them. One-click confirm.

## Editing

Click any contact → edit form. Fields: display name, aliases (nicknames Yorik recognises in chat), relation (mom / dentist / vendor — used by `find_known_provider`), birthday, language preference, salutation preference, business fields (tax ID, IBAN, payment terms), notes, tags.

## Addresses + channels

A contact can have multiple postal addresses (home / work / billing / shipping) and channels (email / phone / WhatsApp / Signal / SMS / website / social). Add via the + buttons inside the contact view.

When you pick a contact for a letter, Yorik uses the highest-priority address (home > work > billing > shipping).

## Sharing with household members

Settings → Users → enable sharing. Contacts have `allowed_roles` — by default the owner sees them only. To share a contact with your partner: edit contact → permissions → add their role.

You can also share specific contacts via the `share_contact` skill in chat: *"teile den kontakt von [name] mit [user]"*.

## Avoiding duplicates

Yorik tries hard to detect duplicates on import (same name + matching channel). If unsure, the modal asks. Manual merge isn't in alpha — for now, delete one and re-edit the other.
