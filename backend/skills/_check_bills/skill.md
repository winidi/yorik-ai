---
name: check_bills
description: List bills with due_date in a window (default = unpaid only)
when_to_use: |
  - Briefing template needs a per-day or per-period bills section
  - User asks "what bills are due this week / tomorrow"
  - For "how much is the X bill" / "when is X due" — call `find_bill_by_name` first, it returns the row to you; this skill only renders cards.

  NOT for "find me a Rechnung / invoice / scanned bill PDF" — that's
  a document search; route to `search_documents`. The word "Rechnung"
  in German means both "bill" (this skill, a row in Yorik's bills
  table) AND "invoice" (a scanned document, search_documents
  territory). When the user wants to OPEN / READ / SEE the document,
  prefer search_documents — even if a bills row also exists, the user
  is asking about the PDF, not the calendar reminder.
inputs:
  start_iso:
    type: string
    required: false
    description: ISO date or datetime. Only bills with due_date >= this are included.
  end_iso:
    type: string
    required: false
    description: ISO date or datetime. Only bills with due_date <= this are included.
  include_paid:
    type: boolean
    required: false
    default: false
    description: Set true to also return already-paid bills (e.g. for the yesterday recap).
outputs:
  bills:
    type: array
    description: |
      Each row has id, name, amount, currency, due_date, recurring, paid, email_message_id, document_id.
      document_id (when set) points to the scanned PDF in the documents store — pass it to read_document(doc_id=document_id) when the user wants to see the actual bill.
  count:
    type: integer
tags: [bills, list, read]
permissions: [admin, member, restricted]
---
# check_bills

Read-only date-bounded query against the bills table. Returns the same shape briefing_routes' BillsList renderer expects.

Reply ONE short sentence with the count; do NOT enumerate bills in prose — the cards/briefing row are the answer.

## What this returns vs what it does NOT return

A row from this skill is a scheduled / expected payment, not proof that a bill document exists.
A row with document_id set has a scan; a row with document_id null has only the table entry.
Never describe a row as "the bill" without checking whether it has a document_id — the user thinks of "Rechnung" as the PDF, not the table row.

## When the user wants to SEE a bill ("zeig mir die Rechnung", "open the bill", "show me the invoice")

If document_id is set, call read_document(doc_id=document_id) and answer from that text.
If document_id is null, call search_documents with the bill name + amount + due date as the query before answering.
If search_documents returns nothing relevant, say plainly that the bill is scheduled but no scan is on file yet — do not surface an unrelated document as if it were the bill.
