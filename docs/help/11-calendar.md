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

In a family household, everybody sees everybody's calendar, read only. It is ordinary sharing: each person finds the tick under Settings → You → Sharing (area "Calendar") and can take it back; admins see nothing extra. New accounts join automatically; for the accounts that already exist, the operator presses **Share calendars in the family** in the same card once and leaves out accounts that are not family. An event marked private shows to the others as "Busy". The Plan calendar of the day planning is never shared.

In the week and day view an event can be dragged to another time or day and resized at its top or bottom edge (15-minute steps, Esc cancels). A recurring event changes its time for the whole series.

Multiple users on the same Yorik instance can share calendars. Settings → Calendars → share → pick which users can see / write. Shared events appear with the calendar's tint colour overlaid on your own day.

## Attendees

When you create an event with multiple participants (chat: *"Termin mit Anna und Markus am Freitag 16 Uhr"*), each named user gets an attendee row. They see the event in their own calendar with an RSVP option. Yorik does NOT auto-invite via external calendar protocols in alpha — attendees are household-internal only.

## Conflict warnings

When you add a new event overlapping with an existing one, Yorik flags it: *"Es gibt bereits zwei Termine am Mittwoch um 16:00 — Sport (16:00–17:00) und Training (16:00–17:30)."* You confirm or pick a different time.

## Searching

Chat: *"Wann ist mein nächster Zahnarzt?"* / *"Was ist nächste Woche Dienstag?"* / *"Wer ist am 15. Juni um 14 Uhr eingetragen?"* — semantic calendar lookup. Yorik also resolves attendees ("Termin mit Anna").

## Categories + colours

Edit a category → pick a colour. Events from each category get that colour stripe on the left. Settings → Task Categories (categories work for events too).

## Bringing your Google calendar (or iCloud, Outlook, Nextcloud)

Calendar sidebar → **Google & Co. übernehmen**. Two ways, both one
direction only; nothing ever goes from Yorik to the other side.

**Subscribe (stays current).** Paste the calendar's secret iCal
address (Google: Settings → your calendar → "Integrate calendar" →
*Secret address in iCal format*). Yorik creates a read-only mirror
calendar and refreshes it every 15 minutes: you keep entering
appointments in Google, and they show up in Yorik, on the family board,
in the search and in the day planning. The address is a secret and is
stored encrypted. A fetch that fails or comes back empty changes
nothing; the dialog shows the state of each subscription, syncs on
demand and ends a subscription (the mirror and its events go, the
source is untouched). A mirror follows your calendar sharing like your
own calendar does.

**Import a file (once).** For moving over: export in Google (Settings →
Import & export → Export; the ZIP holds one .ics per calendar), choose
the file and one of your calendars, look at the preview, import. The
events are yours afterwards. Importing the same file again updates
instead of doubling.

Series: a plain daily, weekly, chosen-weekdays, monthly or yearly
series stays one recurring event. A series with an end, a count, a
longer interval or skipped and moved occurrences is written out as
single events (one year back, two years ahead), with the exceptions
applied. Cancelled events are left out.
