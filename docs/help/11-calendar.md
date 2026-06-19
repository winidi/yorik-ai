---
title: Calendar — events, sharing, attendees
nav_app: calendar
summary: Add events via chat or UI, share calendars between household members, auto-travel-time, attendee RSVPs, conflict warnings.
---

# Calendar — events, sharing, attendees

The calendar is Yorik's flagship surface. Local-only, shared per-user when you want it, with chat-driven add/move/delete.

## Adding events

Three paths:

- **Chat**: *"Trag einen Zahnarzttermin am Donnerstag um 14 Uhr ein"* — Yorik adds it, confirms with a pending-confirmation card.
- **Voice** (global FAB or chat composer mic): same intent, spoken.
- **UI**: Calendar app → click an empty slot → fill the form.

Yorik never adds events without confirmation when `confirm_mutations` is on (default for the beta). The card shows the proposed event + a click-to-undo for 60 minutes.

## Travel time auto-block

When you add an event with a `location` field, Yorik computes drive time from your home address and inserts an "Anfahrt: ..." event before. So a 14:00 dentist in Hannover with 30 minutes of drive automatically gets a 13:30 Anfahrt block. Saves the "forgot to leave on time" failure mode.

You can also explicitly request *"ich muss da hinfahren"* in chat — same auto-block.

## Sharing calendars

Multiple users on the same Yorik instance can share calendars. Settings → Calendars → share → pick which users can see / write. Shared events appear with the calendar's tint colour overlaid on your own day.

## Attendees

When you create an event with multiple participants (chat: *"Termin mit Anna und Markus am Freitag 16 Uhr"*), each named user gets an attendee row. They see the event in their own calendar with an RSVP option. Yorik does NOT auto-invite via external calendar protocols in alpha — attendees are household-internal only.

## Conflict warnings

When you add a new event overlapping with an existing one, Yorik flags it: *"Es gibt bereits zwei Termine am Mittwoch um 16:00 — Sport (16:00–17:00) und Training (16:00–17:30)."* You confirm or pick a different time.

## Searching

Chat: *"Wann ist mein nächster Zahnarzt?"* / *"Was ist nächste Woche Dienstag?"* / *"Wer ist am 15. Juni um 14 Uhr eingetragen?"* — semantic calendar lookup. Yorik also resolves attendees ("Termin mit Anna").

## Categories + colours

Edit a category → pick a colour. Events from each category get that colour stripe on the left. Settings → Task Categories (categories work for events too).
