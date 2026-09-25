---
name: list_bank_accounts
description: List the user's connected bank accounts, own and shared, never someone else's private one.
when_to_use: |
  The user asks what accounts are connected, or you need an account's
  id before calling show_transactions / spending_summary for a
  specific one.
when_not_to_use: |
  Adding a new account — that needs the PIN, which only goes into the
  Settings form, never through chat. Point the user there.
inputs: {}
outputs:
  accounts:
    type: array
    description: Each with id, display_name, iban, space_id (set means shared), last_synced_at, last_sync_error.
cost: 1 SELECT, no network.
permissions: [admin, member, restricted]
side_effects: none — read-only.
tags: [finance, bank, read, app:finance]
category: productivity
---

# list_bank_accounts

Visibility is the standard spaces model: a private account only its
owner sees, a shared one (space_id set, e.g. the household's "Finance"
space) is visible to everyone in that space. No admin exception for
another person's private account.
