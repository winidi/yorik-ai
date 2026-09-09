---
title: Contacts — import, edit, share
nav_app: contacts
summary: Manage your contact hub. Import .vcf or pick from your phone, auto-capture from WhatsApp / email, merge suggestions you confirm, share with household members.
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

One rule decides whether two entries are the same person: a shared email
address or phone number (a WhatsApp id counts as its phone number).
Names never merge anything on their own. Every way of adding a contact
checks this first: the .vcf import, the phone picker, the chat skill,
the New-contact form, and the automatic capture from email and WhatsApp.

Phone numbers are stored in international form (+49…), so "0511 123456"
in a vCard, "+49 511 123456" in a signature and the WhatsApp id
49511123456 all meet.

## From your phone

On an Android phone, the Contacts page shows **From phone**. It opens the
system contact picker; you choose the entries, Yorik shows the usual
import preview (new / merge / conflict) and nothing is written until you
confirm. Only the entries you picked are read. iPhones don't offer this
picker; export a .vcf from the Contacts app and drop it into Yorik.

## Suggestions

Yorik proposes, you decide. A yellow box at the top of the Contacts page
lists what it found:

- **Merge** — the phone number in someone's email signature belongs to a
  WhatsApp-only contact. Pick which entry to keep; the other one's
  channels, addresses and details move over.
- **Add** — a number in an approved contact's signature that nobody has.

Every merge can be undone from the same box. A dismissed suggestion is
not shown again.
