-- Typo-tolerant names in the search (backend/search_vocab.py):
-- the words of a person's senders, subjects, chat and contact names, so
-- "rivertie" can become "riverty" before the search runs. pg_trgm ships
-- with Postgres (contrib); where it cannot be created the table still
-- exists and the correction simply stays off.
CREATE TABLE IF NOT EXISTS search_vocab (
    owner_user_id UUID NOT NULL,
    word          TEXT NOT NULL,
    n             INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (owner_user_id, word)
);

DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    CREATE INDEX IF NOT EXISTS idx_search_vocab_word_trgm ON search_vocab USING gin (word gin_trgm_ops);
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pg_trgm not available (%), search typo correction stays off', SQLERRM;
END $$;
