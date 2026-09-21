-- What the "Schreiben" app writes: letters, invoices, quotes. A draft
-- can change; a final one has its number, its PDF and does not change
-- any more. `content` is JSON: a letter's subject and text (HTML from
-- the editor), an invoice's lines and dates. `recipient` is JSON too
-- ({contact_id, name, address_lines}); it is frozen when finalised.
-- Named written_documents because docs.documents is the Paperless mirror.
CREATE TABLE IF NOT EXISTS written_documents (
    id            BIGSERIAL PRIMARY KEY,
    user_id       UUID NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('letter', 'invoice', 'quote')),
    status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'final')),
    letterhead_id BIGINT,
    title         TEXT NOT NULL DEFAULT '',
    recipient     TEXT NOT NULL DEFAULT '{}',
    content       TEXT NOT NULL DEFAULT '{}',
    number        TEXT,
    doc_date      TEXT,
    pdf_path      TEXT,
    paperless_doc_id INTEGER,
    source_document_id BIGINT,          -- the quote an invoice was made from
    created_at    TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    updated_at    TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    finalised_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_written_documents_user ON written_documents(user_id, status, updated_at);
