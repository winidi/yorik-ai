---
name: read_document
description: Fetch the full extracted text of one document by id (capped at 40k chars)
when_to_use: |
  Pair with find_document. find_document gives you ranked snippets
  across many docs; read_document gives you the WHOLE text of ONE doc.

  Call this when:
  - You have a doc_id (from find_document, from a document bucket
    seed, from a previous tool call) AND you need to extract structured
    facts from the body (landlord name, contract dates, IBAN, amounts).
  - The user dropped several documents into the chat's document bucket
    and asked for something that needs the actual contract text — e.g.
    "create a Mietkündigung from these docs" needs the Mietvertrag's
    landlord + start date.

  Do NOT use:
  - For free-text search across many docs — that's find_document.
  - To stream the raw bytes back — there's no UI for that; the
    chat just shows text.

  Output is capped at 40_000 chars (~10k tokens) so one fat lease
  can't blow your context window. When truncated=True, ask the user
  for a more specific question OR scope down which sections you need.
inputs:
  doc_id:
    type: integer
    required: true
    description: The id of the document to read (from find_document or a document-bucket seed).
outputs:
  ok:
    type: boolean
  doc_id:
    type: integer
  title:
    type: string
  mime_type:
    type: string
  text:
    type: string
    description: Concatenated text from all chunks in chunk_index order. May be truncated.
  total_chars:
    type: integer
  truncated:
    type: boolean
  chunk_count:
    type: integer
cost: 1 SELECT per call (cheap). No LLM, no network.
permissions: [admin, member, restricted]
side_effects: none — read-only
tags: [documents, rag, read]
---

# read_document

Concatenates the indexed chunks for one doc and returns the full text.
Use it as the second step in find_document → read_document → act.
