---
title: Briefing — your daily summary
nav_app: briefing
summary: Morning / evening summary of today's events, tasks, bills, photos, email. Customisable, runs locally, never sends data out.
---

# Briefing — your daily summary

The Briefing app is your at-a-glance view: what's coming today, what's overdue, what's new since yesterday.

## Three tabs

- **Yesterday** — recap of the previous day. Used to confirm "did I do X" / "what came in overnight".
- **Today** — primary tab. Events, tasks, bills due, photos, email.
- **Tomorrow** — preview of tomorrow's events + tasks coming due. Useful for "should I prep something tonight".
- **Recap** — weekly summary (Monday for last week).

## What's in a briefing

- **Calendar**: every event for the day, with times + locations + travel-time hints.
- **Tasks**: today's + overdue. One-click mark-done.
- **Bills**: anything due in the next 7 days. Click → open the bill PDF if a scan exists.
- **Photos**: recent uploads since yesterday (good for "remember the day").
- **Email**: actionable mail digest — new threads, unanswered replies, calendar invites.

## Generating a briefing on demand

Ask Yorik in chat: *"gib mir einen Überblick über meinen Tag"* — produces the briefing inline. The briefing is also pre-rendered every morning at a fixed hour (Settings → Briefing → wake-up time).

## Customising

Settings → Briefing → toggle sections on/off, set the wake-up time, add custom sections (community-contributed briefing templates). Each section can be hidden if you find it noisy.

## Voice briefing

Hit the voice FAB and say *"morgen-briefing"* — Yorik reads the briefing out loud (v0.2 — needs TTS). For alpha, text-only via the briefing endpoint.

## How it's built

Briefings are server-rendered from skills (`check_calendar`, `check_tasks`, `check_bills`, `find_photo`, `email_digest`) called in parallel, formatted into a single page. Stateless — refreshing always gives you fresh data.
