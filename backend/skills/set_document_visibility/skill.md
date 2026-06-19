---
name: set_document_visibility
description: "Change a Paperless document's visibility — private, business, or shared."
when_not_to_use: |
  Don't use to change a CONTACT's sharing — that's `share_contact` / `update_contact(space=...)`. Don't use to change a calendar event's visibility — that's `update_calendar_event(visibility=...)` for per-event privacy, or move the calendar to a different space. This skill is documents only.
when_to_use: |
  - User wants to share a document with the household ("teile den Mietvertrag mit der Familie") → visibility="shared".
  - User wants to mark a document as business-shared ("die Rechnung ist für alle im Geschäft") → visibility="business".
  - User wants to lock a document back to private ("nur ich soll das sehen") → visibility="private".

  ALWAYS call find_document first if you don't know the document_id. Quote the document's title in your reply so the user knows which one you changed.

  Only the document's owner (or admin) can change visibility. Members get a clear permission error if they try on someone else's document.
inputs:
  document_id:
    type: integer
    required: true
    description: Paperless document id, as returned by `find_document`.
  visibility:
    type: string
    required: true
    description: "'private' | 'business' | 'shared'."
outputs:
  document_id:
    type: integer
  visibility:
    type: string
    description: The visibility level now in effect.
  tag_ids:
    type: array
    description: Paperless tag ids attached to the document after the change.
permissions: [admin, member]
side_effects: |
  PATCH on the Paperless API to update the document's tags. Emits a refresh_data ui_action so the documents app re-renders.
tags: [documents, sharing, write]
---

# set_document_visibility

Tag-based visibility wrapper. Three levels (private / business / shared) map to Paperless tags + groups (see backend/paperless_visibility.py for the model).

Owner-check happens before the Paperless PATCH. If a member tries to change someone else's document, this skill raises a permission error and the chat surfaces it.
