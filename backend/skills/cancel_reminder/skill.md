---
name: cancel_reminder
description: Show the user's open reminders, or cancel one of them by its id.
when_to_use: |
  - The user no longer needs a reminder that remind_me set ("forget the reminder about the dentist").
  - The user asks which reminders are still open.
  - Call it without reminder_id to see the open ones, then with the id of the one the user means.
when_not_to_use: |
  - Calendar events or tasks: that is delete_calendar_event or delete_task.
inputs:
  reminder_id:
    type: integer
    required: false
    description: The ONE reminder to cancel, from this skill's list; leave out to list the open ones.
outputs:
  reminders:
    type: array
    description: The open reminders (id, title, due_local), when no id was given.
  cancelled:
    type: boolean
permissions: [admin, member, restricted]
tags: [notifications, reminders, write]
---
# cancel_reminder
Lists the caller's own open reminders, or cancels one; a cancelled reminder never rings. Other people's reminders are never shown or touched.
