-- WhatsApp semantic search on Postgres.
--
-- docs.wa_chunks was carried over from SQLite without its vector twin
-- (wa_vec, a sqlite-vec virtual table), so on every Postgres install
-- index_message() failed on INSERT INTO wa_vec and search() returned
-- nothing. Same shape as paperless_chunks / document_chunks: the
-- embedding is a column on the row, cosine ivfflat index on top.
ALTER TABLE docs.wa_chunks ADD COLUMN IF NOT EXISTS embedding vector(384);
CREATE INDEX IF NOT EXISTS idx_wa_chunks_embedding
    ON docs.wa_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
