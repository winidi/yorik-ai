---
name: notify
description: Leave a message in the user's Yorik notification bell (and push it to their phone) when a result is ready or something needs their attention.
when_to_use: |
  Use it from an outside agent when a long task finished, a scheduled job produced something, or the user should look at something later; the message reaches their phone even when Yorik is closed.
  Keep `title` to one line and `body` to two or three sentences; put the rest where the user can find it and pass that as `url` (a Yorik route like /r/documents, or an http link).
  Do not use it for the answer to a question the user just asked in this conversation; answer directly instead.
when_not_to_use: |
  Anything the user is reading right now in the chat.
inputs:
  title:
    type: string
    required: true
    description: One short line naming what is ready, without quotes or colons.
  body:
    type: string
    required: false
    description: Two or three sentences at most.
  url:
    type: string
    required: false
    description: Where the bell entry leads when tapped (Yorik route or http link).
outputs:
  notification_id:
    type: integer
  pushed_devices:
    type: integer
cost: instant
permissions: [admin, member]
side_effects: Creates a notification for the calling user and pushes it to their subscribed devices.
tags: [notifications, agent, mcp-first-class]
category: system
---

# notify

The reverse channel: the workstation agent reports back into Yorik
instead of into a messenger. Notifications belong to the calling user
(the token owner over MCP); nobody can notify somebody else.
