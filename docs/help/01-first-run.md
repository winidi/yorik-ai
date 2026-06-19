---
title: First run — what to do after install
nav_app: home
summary: What a new user should do after Yorik finishes installing and the LLM is connected. Five-minute starting walk-through.
---

# First run — what to do after install

Yorik is installed and your LLM responds to chat. You've seen the welcome screen. Here's the recommended next 5–10 minutes.

## 1. Set your name + address

Go to **Settings → Profile**. Fill in:

- Your name (and business name if you invoice as a business)
- Your address — street, postcode, city
- IBAN if you'll generate invoices
- USt-IdNr / Steuernr if you have them

Why first: every letter, invoice, and email Yorik composes pulls the sender block from here. Fill it once and every document is correct.

## 2. Pick a few demo entries

On the **Home** dashboard, click **"Seed demo data"** (or run `curl -X POST http://localhost:8000/api/demo/seed -b cookies.txt`).

You'll get a week of fictional events, a few tasks, a couple of bills, and a welcome notification. Date-shifted around today so nothing feels stale. Removable in one click from **Settings → Demo**.

If you'd rather not see anyone else's data: skip this. Yorik works fine empty.

## 3. Try the chat

The chat is your main interaction surface — keyboard or voice. Try:

- *"Was steht heute an?"* — Yorik reads the calendar + tasks + bills and gives you the day at a glance.
- *"Trag einen Zahnarzttermin für Donnerstag 14 Uhr ein"* — adds an event. (Yes, it asks for confirmation before mutating anything.)
- *"Wer ist [name]?"* — looks up the contact.

If a chat answer goes wrong, click the message → **debug bundle**. Shares the tool trace so you can file a useful issue.

## 4. Connect the services you'll use

Most are optional, but if you want full Yorik:

- **Paperless** for documents — see `paperless`
- **Immich** for photos — see `immich`
- **Tailscale** for remote phone access — see `tailscale`
- **WhatsApp bridge** if you want chat ingestion — see `whatsapp`
- **Email IMAP/SMTP** for inbox features — see `email`

Each connector self-checks on launch. The **Home** dashboard shows green/yellow/red dots so you can see at a glance what's up.

## 5. Skip onboarding overall?

That's fine. Yorik works one feature at a time — start with chat + calendar, add the rest later as needed. There's no "complete the setup" gate you have to clear.

## What to ask the chat next

Ask Yorik directly for any of the topics above. Examples:

- *"Wie verbinde ich Paperless?"*
- *"Wie richte ich Tailscale ein?"*
- *"Wie hänge ich Fotos aus meinem Handy in Yorik ein?"*

Yorik calls the `yorik_help` skill internally and reads from these docs. So whatever you ask about, the answer is grounded in shipped instructions — not a guess.
