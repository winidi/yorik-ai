---
name: yorik
description: "Use the Yorik home server (mcp__yorik__* tools) for the household's calendar, tasks, contacts, documents, photos, bills, letters and email — and know its confirmation rules."
version: 1.0.0
author: Yorik
license: AGPL-3.0-or-later
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Yorik, Household, Calendar, Tasks, Contacts, Documents, Letters, MCP]
    related_skills: []
---

# Yorik

Yorik is the household's data server. Every `mcp__yorik__*` tool is one
Yorik skill, run as the person who owns the API token. Yorik keeps the
data, the permissions and the audit trail; you are the assistant on top.

## When to reach for Yorik

- Anything about **appointments, dates, "what's on"**: `check_calendar`,
  `find_event_by_title`, `add_calendar_event`, `update_calendar_event`,
  `propose_meeting_times`, `block_travel_time`.
- **To-dos and chores**: `check_tasks`, `add_task`, `update_task`,
  `find_task_by_title`, `list_subtasks`.
- **People**: `find_person` (household members via `source='household'`,
  everyone else via contacts), `add_contact`, `add_contact_address`,
  `add_contact_channel`, `read_my_profile` for the user's own data.
- **Documents and scans** (Paperless): `search_documents`, `read_document`,
  `read_document_vision`, `universal_search` across mail, WhatsApp, docs, photos.
- **Photos** (Immich): `find_photo`.
- **Bills and money**: `check_bills`, `add_bill`.
- **Letters, cancellations, invoices** (German templates, GoBD):
  `pick_compose_template` → `compose_check_recipient` →
  `compose_check_template_args` → `compose_draft`.
- **Email and WhatsApp summaries and drafts**: `email_briefing`,
  `email_draft` (reply inside an existing thread), `prepare_email`
  (new email: recipient, subject, body — stages a card with a Send
  button; only call `send_prepared_email` after the user says to send),
  `whatsapp_briefing`, `whatsapp_draft`. `delete_email` deletes one email
  after the user's yes; several emails are several calls, each with its own yes.
  `list_email_accounts` says which mailboxes Yorik has and their provider
  (Gmail, Proton Mail …); to send from one, pass its address or provider as
  `from_email` to `prepare_email`.
- **Day planning**: `plan_my_day` (material: fixed events, open tasks,
  carry-over, yesterday's review) → draft with the user, iterate →
  `plan_day` writes tasks + Plan-calendar blocks (only after "go");
  `day_review` in the evening. Yorik owns the plan; do not create the
  tasks and events one by one yourself.
- **Reminders**: `remind_me` sets one; `cancel_reminder` lists the open
  ones (no id) or cancels one by its id.

Do not answer calendar, task or contact questions from memory when Yorik
is connected; read from it.

Never bypass the `mcp__yorik__*` tools to reach Yorik's data directly
(its database, its app): the yorik-guard plugin refuses such attempts and
states the rule itself. If an operation has no MCP skill, say in one
sentence which step is missing instead of trying a workaround.

## How to call a skill

Every Yorik skill is its own tool (`mcp__yorik__<skill>`); describe the
one you need and call it. Do not call `skill_view` on Hermes' own skill
named "yorik" — this file is already loaded.

1. Call the skill with exactly the argument names from its schema.
   A wrong argument name comes back as an error with the valid keys;
   fix and retry once, do not guess repeatedly.
2. `mcp__yorik__skill_view(name=...)` only when a skill has rules you
   need before calling: letters (compose_*), deletions, relative dates.
3. Creates and updates are applied immediately. Their result may carry a
   `pending_id` — that is an undo handle, NOT something to confirm. Never
   call `pending_confirm` after a create; only deletions have a card.
4. Dates are ISO 8601 local time (`2026-09-15T10:00:00`). Resolve relative
   dates yourself and state the resolved date in your reply.
5. `whoami` tells you which account the token acts as.

## When the MCP tools are not loaded

A session can start without the `mcp__yorik__*` tools (tool_search
returns nothing; tool_call says the name is unknown). Do not guess at the
data and do not reach for it another way; tell the user in one sentence
that Yorik is not connected in this session.

## Reporting back

When a task takes long or runs from cron, finish with
`mcp__yorik__notify(title, body, url)`: the person gets it in Yorik's
bell and as a push on their phone. Use it instead of a messenger message.

## Contact lookup pitfalls

- `list_contacts_for_picking` returns the FULL address book (hundreds
  of contacts) — do not call it to pick one contact; use
  `find_person(query=...)`, which matches name/relation/email and
  returns a small candidate list. Only if no query fits does the full
  list become necessary.
- `find_person` is often ambiguous (several people share a surname).
  The result then carries a `_llm_hint` listing every candidate with
  id, email, whatsapp jid and relation. Do NOT ask for the number —
  all values you need are in that hint; just ask one short
  disambiguation question ("Welche Anna?") when the user's words
  don't resolve it, then take the channel value straight from the
  hint.
- Many contacts have only a WhatsApp channel and no email. Before
  drafting an email, check the candidate actually has an `email`
  channel; otherwise pick another contact or tell the user.
- A staged draft replaces any previous staged draft
  (`replaced_previous_draft: true`) — there is one pending email card
  at a time.
- The same person often exists as TWO contact rows (email-derived row and
  WhatsApp-number row). Yorik reports this as an `unlinked` suggestion in
  the result of `person_context`, `read_email` or `whatsapp_read`. Do not
  act on it yourself; ask the user once whether both are one person, then
  pass their answer to `decide_contact_suggestion(suggestion_id, accept,
  keep_id)` — the id comes from the suggestion text. A yes merges both rows
  into one contact holding every channel; the merge can be undone later in
  Yorik under Contacts → Suggestions.

## Confirmations

Yorik never deletes on your say-so. A delete skill stages the action and
returns `pending_confirmation` with a `pending_id` and a `preview`.

- Default: the owner confirms in the Yorik app. Show the preview, then
  say that the card is waiting in their Yorik notification bell. Do not
  call `pending_confirm`; it is refused for this account.
- If the user changes their mind, `pending_cancel(pending_id)` withdraws it.
- Only if the account allows agents to confirm deletions (Yorik Settings →
  Beta safety) does `pending_confirm(pending_id)` run the deletion, and then
  only after the user agreed to the preview in this conversation.

Creates and updates apply at once and may return a `pending_id` too;
`pending_cancel` undoes them within the hour.

## Tone

Reply in the user's language. Quote titles, dates and amounts from the
tool result; never claim success for a call that returned `isError`.
