-- One semantic index for everything the household search covers
-- (docs/plans/2026-09-19-suche-ueberall.md). The index knows source
-- and row only; visibility is checked at query time by joining the
-- source table, so a changed share needs no rebuild.
CREATE TABLE IF NOT EXISTS search_chunks (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT    NOT NULL,   -- email | whatsapp | tasks | contacts | events | recordings | drafts
    row_id       BIGINT  NOT NULL,
    chunk_no     INTEGER NOT NULL DEFAULT 0,
    text         TEXT    NOT NULL,
    content_hash TEXT    NOT NULL,   -- hash of the whole row text, same on every chunk of a row
    embedding    vector(384),
    indexed_at   TEXT    NOT NULL,
    UNIQUE (source, row_id, chunk_no)
);
CREATE INDEX IF NOT EXISTS idx_search_chunks_source_row ON search_chunks (source, row_id);
