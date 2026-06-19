---
title: Email — IMAP / SMTP setup
nav_app: settings
nav_query:
  tab: connectors
summary: Connect your IMAP inbox so Yorik reads + classifies incoming mail. Send drafts via SMTP. Multiple accounts supported.
---

# Email — IMAP / SMTP setup

Yorik reads your inbox via IMAP and sends via SMTP. No webmail, no OAuth dance — direct protocol access. Credentials encrypted locally with Fernet.

## Adding an account

Settings → Connectors → **Email** → **Add account**.

Fill in:

- **IMAP host + port + SSL**: e.g. `imap.gmail.com:993` SSL.
- **IMAP username + app password**: for Gmail / iCloud / Yahoo, use an app-specific password (not your real account password).
- **SMTP host + port + SSL/STARTTLS**: e.g. `smtp.gmail.com:587` STARTTLS.
- **From address**: the email shown in the "From" field of sent mail.

Yorik tests both legs on save. Green check = working. Red = bad credentials or unreachable host.

## App passwords (Gmail / iCloud / others)

Most providers require app-specific passwords for IMAP/SMTP — your real password won't work even if 2FA is off.

- **Gmail**: myaccount.google.com → Security → 2-Step Verification (must be on) → App passwords → generate one.
- **iCloud**: appleid.apple.com → Sign-In and Security → App-Specific Passwords → generate.
- **Outlook.com**: account.microsoft.com → Security → Advanced security → App passwords.

Paste the generated password into Yorik's password field.

## What Yorik does with email

- **Reads**: incoming mail is classified (bills, calendar invites, personal, marketing, etc.). Bills get auto-extracted into the bills app. Calendar invites become events. Marketing gets de-prioritised in the briefing.
- **Briefing**: morning summary mentions new + actionable email since yesterday.
- **Drafts**: chat agent's `email_draft` skill creates drafts you can review + send.

## Sending email

Yorik never auto-sends. Drafts always require a "Send" click in the email app or Compose → Send dialog. Confirmation modal appears for the first send per account per session.

## Multiple accounts

Add as many as you want. Each has its own connector entry. The briefing combines all; the email app has a per-account filter.

## Privacy

Email content is stored in Yorik's local DB (`email_messages`). Never forwarded anywhere else unless YOU configure something. The classifier runs locally — no cloud calls.

## Troubleshooting

- **IMAP auth failed**: 99% of the time it's "use an app password" — see above.
- **Mail arrives but Yorik doesn't see it**: check the IMAP poll cycle (Settings → Connectors → Email → poll interval; default 5 min).
- **SMTP STARTTLS error**: some providers want SSL on port 465 instead of STARTTLS on 587. Try the other config.
