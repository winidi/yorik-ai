-- One row per Paperless document, so the documents share the search
-- index (search_chunks, Qwen3-Embedding) with mail, WhatsApp and the
-- rest. Until 2026-09-27 they were embedded only by the small English
-- MiniLM (docs.paperless_chunks, 384 dims): "was kostet unser server,
-- da kam doch ne rechnung" never reached "Your invoice … RS 4000".
-- Filled by paperless_ingest.ingest_one; whether a person may see a
-- document is still decided by Paperless with that person's token.
--
-- Additive only: one new table.

CREATE TABLE IF NOT EXISTS docs.paperless_documents (
    id            INTEGER PRIMARY KEY,          -- the Paperless document id
    title         TEXT NOT NULL DEFAULT '',
    correspondent TEXT NOT NULL DEFAULT '',
    doc_type      TEXT NOT NULL DEFAULT '',
    doc_date      TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL DEFAULT '',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
