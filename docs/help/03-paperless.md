---
title: Adding documents — Paperless workflow
nav_app: documents
summary: How Paperless integrates with Yorik for document storage. Upload from app, scan workflow, OCR, search, bills→docs linking.
---

# Adding documents — Paperless workflow

Yorik bundles Paperless-ngx as the document store. PDFs, Word files, scanned images, emails — they all land here, get OCR'd, and become searchable from Yorik's chat + Documents app.

## Where Paperless runs

Bundled by default. `bash start.sh` brings up the Paperless container (paperless-web, paperless-db, paperless-broker). Web UI at `http://localhost:8010` if you ever need it directly.

If Paperless was already running on your machine when you ran `start.sh`, Yorik detected it and skipped bundling — it'll talk to your existing instance instead.

## Three ways to add a document

### A. Drop it in the Documents app

Open Yorik → **Documents** app → drag-and-drop file onto the page (or click the upload button on mobile). Goes through Yorik's `/api/documents/upload` → Paperless ingests → OCR runs in the background.

OCR languages: by default `deu+eng`. Tesseract has German + English language packs bundled. To add more: `docker exec yorik-paperless-web apt-get install tesseract-ocr-fra` (or whatever ISO code), then `docker restart yorik-paperless-web`.

### B. Email it in

Paperless polls IMAP boxes if configured. Set this up at Settings → Connectors → Paperless → IMAP. Useful for forwarding invoices from your inbox.

### C. Drop it in `data/paperless/consume/` (BULK IMPORT — fastest path for hundreds of docs)

This is the fastest workflow when you have a backlog of existing documents.

1. Find the consume folder: `~/yorik-ai/data/paperless/consume/`.
2. Copy or move all your PDFs / scanned images into it. No quantity limit — Paperless processes them one at a time in the background. Subfolders are flattened on ingest.
3. Each document takes 5–30 seconds to OCR + index (depends on page count + scan quality). 600 documents = ~30–60 minutes total.
4. After Paperless is done, click the **refresh icon** in Yorik's Documents-app sidebar so Yorik's mirror catches up immediately (otherwise its scheduled reconciler runs only every 6 hours).

Also useful for scripted ingestion: a scanner that saves to a network share mounted at `data/paperless/consume/`, a cron that drops email attachments there, etc.

**Watch progress in real time** at `http://localhost:8010` (Paperless web UI, also accessible from Yorik's Documents-app sidebar). The "Documents" count climbs as ingest completes.

## Scan workflow (recommended setup)

For physical mail:

1. **Phone**: install the Paperless-Mobile app (Android / iOS). Configure with your Yorik machine's IP (or Tailscale hostname).
2. **Scan**: photo of the document → app uploads directly to Paperless.
3. Yorik picks it up automatically on the next document search.

Tags: configure rules at Paperless web UI (`localhost:8010`) → Settings → Workflows. Auto-tag invoices, contracts, tax docs — the rules are per-user.

## Finding documents in chat

Ask Yorik:

- *"Hab ich irgendwo einen Mietvertrag liegen?"* — searches semantically + by keyword.
- *"Zeig mir die Stromrechnung von letztem Monat"* — combines bill metadata with document hits.
- *"Wie hoch ist der Betrag in dem Vertrag von Mustermann?"* — Yorik reads the doc and answers from the OCR text.

Behind the scenes: hybrid search (semantic embeddings + Paperless FTS), fused via RRF. Both legs degrade gracefully if one is down.

## Bills ↔ documents linking

When Yorik adds a bill (via chat or auto-extraction from email), it stores the scanned-PDF reference in `bills.document_id`. So *"zeig mir die Rechnung von Stadtwerke"* opens the actual PDF, not just the metadata row. If the bill was added without a scan, `document_id` is null and Yorik says so honestly ("ich hab den Termin gespeichert, aber das Dokument noch nicht").

## Privacy

Paperless data lives in `data/paperless/` on the Yorik host. Never leaves the machine unless YOU configure something to push it out. The Documents app and chat queries are read-only from Paperless's perspective — Yorik never deletes a document via the API.

## Troubleshooting

- **Paperless thumbnails 400 Bad Request**: usually a stale CSRF cookie. Hard-refresh the browser.
- **OCR not happening**: check `docker logs yorik-paperless-web` for tesseract errors. Often it's the consume folder permissions.
- **Search returns nothing for a known doc**: Paperless might still be OCR'ing. Wait 1–2 minutes; the document appears once OCR finishes.

## Direct Paperless access

If you need the full Paperless UI (workflows, tags, correspondents, custom fields): `http://localhost:8010`. Login uses `PAPERLESS_ADMIN_PASSWORD` from your `config.env` (or `data/.paperless-admin-pw` after first install). Yorik's API token to Paperless is stored encrypted; you don't need to know it.
