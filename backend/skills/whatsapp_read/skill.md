---
name: whatsapp_read
description: "Read one WhatsApp chat around a search hit, or its latest messages, to see what someone wrote."
when_to_use: |
  - A search hit lies in WhatsApp (universal_search gives id and chat_jid): read around it with around_message_id before you answer, and check it is the right person and still current.
  - The user asks what someone wrote ("what did Jan write?"): read the latest messages of that chat (chat_jid, or contact_id from find_person).
  - A calendar file (.ics) in a chat: its title, time and place come with the message when WhatsApp still has the file.
when_not_to_use: |
  - An overview of all chats of the last hours — that's whatsapp_briefing.
  - Writing a reply — that's whatsapp_draft.
inputs:
  around_message_id:
    type: integer
    required: false
    description: The id of a WhatsApp search hit; returns the messages before and after it.
  chat_jid:
    type: string
    required: false
    description: The chat to read (from a search hit or a contact's WhatsApp channel).
  contact_id:
    type: integer
    required: false
    description: A contact from find_person, when no chat_jid is known.
  before:
    type: integer
    required: false
    default: 3
    description: Messages before the hit (max 25).
  after:
    type: integer
    required: false
    default: 3
    description: Messages after the hit (max 25).
  last:
    type: integer
    required: false
    default: 10
    description: Without a hit, how many of the latest messages (max 25).
outputs:
  messages:
    type: array
    description: Each with id, when, who ("me" = the user), text, media, calendar (for .ics), hit (the searched message).
  messages_after_this_window:
    type: integer
    description: How many newer messages follow — a large number means the chat went on and may have changed things.
cost: 1 SELECT; a calendar file costs one fetch from the WhatsApp bridge.
permissions: [admin, member, restricted]
side_effects: none — read-only.
tags: [whatsapp, read, app:whatsapp]
category: communication
---

# whatsapp_read

Only the calling person's own chats (wa_messages.owner_user_id). After
reading, the hint asks the model to check the hit — right person, still
current, not replaced by a later message — and to search on if it does
not fit.
