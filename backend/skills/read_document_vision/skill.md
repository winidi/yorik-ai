---
name: read_document_vision
description: Render PDF pages and let the multimodal LLM read them when OCR text is garbled.
when_to_use: |
  Trigger A — read_document returned an OCR-garble warning (text contains the WARNING hint, ratio > 0.25); the printed letterhead / fancy font / faint scan defeated Tesseract.

  Trigger B — the question needs layout reasoning the flat OCR text destroys (table rows + columns, form field positions, side-by-side German/English contract clauses).

  Trigger C — read_document returned plausible text but the user asks for a specific value ("was habe ich für X bezahlt?") that you can't find in the OCR; the value might be on a page Paperless OCR'd poorly even if other pages came through clean.

  Pass `question="<focused question>"` whenever you have one — the model answers it directly and returns a short reply instead of a transcription wall.

  One doc_id per call — to read multiple docs, call this skill multiple times in parallel; there is no batch form (no doc_ids array).

  Aim for page ranges around the section that likely holds the answer (Vertrag amounts → first few pages; Rechnung totals → last page; Kalkulationsblatt → all pages). The skill caps at 15 pages per call regardless.
inputs:
  doc_id:
    type: integer
    required: true
    description: "Same id space as read_document — native upload id or Paperless doc id."
  pages:
    type: string
    required: false
    default: "1-10"
    description: "Page range to render — '1-10', '5', '3-7'. Caps at 15 pages per call."
  question:
    type: string
    required: false
    description: "Focused question for the model; when given, reply is the answer (short). When omitted, reply is a full transcription/summary."
outputs:
  ok:
    type: boolean
  text:
    type: string
    description: "Model's reply — either the answer to `question` or a transcription/summary."
  source:
    type: string
    description: "'native' (uploaded to Yorik) or 'paperless'."
  pages_read:
    type: integer
permissions: [admin, member, restricted]
side_effects: "renders pages to /tmp briefly, one LLM call (~2s per page)"
cost: "~2s/page on local 9B + BF16 mmproj; ~1280 vision tokens per page against context"
tags: [documents, vision, ocr, lookup]
---

# read_document_vision

The vision-LLM escape hatch when Paperless's Tesseract OCR mangles a document. Same Qwen3.5-9B endpoint Yorik uses for everything else, just with image content blocks. Pages are rendered with pdftoppm at 150 dpi and base64-encoded into a single multimodal message.

Use this AFTER read_document hits a garble warning, or when a value-seeking question can't be answered from the OCR text alone. For digital-native PDFs with clean OCR, read_document is faster and cheaper — don't reach for vision when the flat text already has the answer.

Be specific with `question` — the model answers focused questions in 1-3 lines instead of dumping a 10-paragraph transcription.
