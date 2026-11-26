---
name: list_email_accounts
description: The user's own email accounts, with provider (Gmail, Proton Mail …), default and last sync.
when_to_use: |
  - The user names a mailbox by its provider or address ("from my Gmail", "my Proton account").
  - Before telling the user an account or mailbox is not connected: check it here.
  - An email the user knows of is not found: last_sync_at and last_error show if that account is syncing.
when_not_to_use: |
  - Reading or searching mail: that is find_email_by_subject, read_email or email_briefing.
inputs: {}
outputs:
  accounts:
    type: array
    description: Each has email, name, provider, is_default, enabled, last_sync_at, last_error.
  count:
    type: integer
cost: instant — no LLM call, no network.
permissions: [admin, member]
side_effects: none — read-only
tags: [email, accounts]
---
# list_email_accounts
Lists only the caller's own accounts, never anyone else's. To send from one, pass its email as from_email to prepare_email.
