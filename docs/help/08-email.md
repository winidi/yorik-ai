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

## Your mailbox stays the original

Yorik works like a mail program (Thunderbird, the mail app on your phone): the mail stays on your provider's server, and Yorik keeps a copy in step with it.

- **How much Yorik keeps**: when you add an account, and later under Email → Settings → *Keep in Yorik*: the latest 200 mails (in every other folder the latest 50), the last 3 months, the last year, or everything. Older mail simply stays on the server; a wider choice brings it in, quietly, in the background.
- **In step with the server**: new mail arrives at once. Every 15 minutes, and whenever you press **Check now**, Yorik compares every folder with the server: it fetches whatever is missing, takes over *read* and *flagged* from your other devices, and lets go of mail you deleted or moved elsewhere (a moved mail shows up in its new folder).
- **Old mail arrives quietly**: mail brought in by an import or a check makes no bell entry, no draft and no suggestion; only mail from the last two days counts as new.
- **Nothing is skipped**: a mail Yorik cannot read or store is tried again on every check. After a few tries it is listed under the account ("… could not be read — they are safe on the server"); **Check now** gives them another round.
- **Deleting**: *Delete* moves the mail to the Trash on the server. Deleting in the Trash deletes that mail for good — only that one. When the mail cannot go to the Trash (no Trash folder, the server refuses), nothing is deleted and you see why.
- **Gmail**: "All Mail", "Important" and "Starred" hold copies of your other mail. Yorik shows each mail once; mail you archive in Gmail stays visible.

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
- **Mail arrives but Yorik doesn't see it**: open Email → Settings and look at the account: *In step with the server* with the time of the last check, or *Bringing mail in — N to go*. Older than your *Keep in Yorik* choice? Widen it. Otherwise press **Check now**.
- **SMTP STARTTLS error**: some providers want SSL on port 465 instead of STARTTLS on 587. Try the other config.
