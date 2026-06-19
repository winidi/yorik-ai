---
name: search_documents
description: Search the user's document library (Paperless + native uploads) — hybrid semantic + keyword.
when_to_use: |
  Any question whose answer is likely in a filed document — contracts,
  invoices, letters, manuals, school papers, warranties, notes. Search
  is semantic, so paraphrase the user's words when the document might
  use different terminology ("Stromrechnung" finds documents about
  "Energieabrechnung", etc.).

  USE THIS for the German word "Rechnung" when the user wants the
  scanned document — "find a Rechnung", "show me the Rechnung from
  X", "what's the IBAN on the Rechnung", "Rechnung aufmachen". The
  same word in German also means a bills-table row (check_bills),
  but if the user asks to FIND, OPEN, READ, or extract anything from
  it, they mean the PDF — this skill, not check_bills.

  Two modes:
    - With a query → hybrid search across Paperless + native uploads.
    - With empty query → recent N documents across both sources. Use for "zeig mir ein Dokument" / "irgendein Brief" / open-ended browse.

  After the call, give a ONE-line reply ("3 Treffer, siehe Karten unten" or equivalent). The chat renders each hit as a clickable card — do NOT enumerate titles, correspondents, or dates in prose.

  Follow-up: if the user asks WHAT is in a hit ("wie hoch ist der Betrag", "was steht drin"), call `read_document(doc_id=<id>)` and answer from the returned text — do NOT navigate to the documents app as a substitute.
when_not_to_use: |
  Reads of bills/tasks/events/contacts — those have dedicated skills (`check_bills`, `check_tasks`, `check_calendar`, `find_person`) that emit their own cards. Documents are the PDF surface; bills are the metadata surface; these are different things even when the underlying invoice is the same.

  Photo / Immich queries — call `find_photo`.
inputs:
  query:
    type: string
    required: false
    default: ""
    description: Natural-language search (semantic — meaning matches, not just keywords). Empty string returns the most recent N documents.
  k:
    type: integer
    required: false
    default: 5
    description: How many documents to return (1-20).
outputs:
  hits:
    type: array
    description: |
      One element per match with doc_id, doc_title, correspondent,
      doc_date, snippet, distance, source ('paperless' | 'local'),
      match_type ('semantic' | 'fts' | 'hybrid') when in search mode.
  total:
    type: integer
    description: Total document count in the library (recent mode) or number of hits (search mode).
side_effects: Emits a `documents_found` UI action so the chat can render cards.
cost: 1 embedding call (~50ms) + sqlite-vec ANN scan + Paperless FTS query (parallel)
permissions: [admin, member, restricted]
tags: [documents, paperless, search, rag]
---

# search_documents

Hybrid RAG over the user's two document indexes — native uploads
(`documents` + `document_chunks`) and the Paperless mirror
(`paperless_chunks` + `paperless_vec`) — merged so the caller never has
to know which source a hit came from. Search mode fuses semantic +
Paperless FTS via Reciprocal Rank Fusion; recent mode returns the
newest N across both sources.

Degrades gracefully when one engine is down — semantic-only or FTS-only
results still flow with a leg-status caveat so the LLM can hedge its
reply accurately ("only keyword search ran"). Both engines down with
zero hits surfaces as an explicit "store unreachable" message rather
than the misleading "no documents found".

Hit shape carries `source` ('paperless' | 'local') so the chat's
preview/download buttons can route to the right backend endpoint.
