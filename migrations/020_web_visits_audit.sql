-- Migration 020: web search/fetch audit log
--
-- Every web_lookup + web_fetch call inserts a row. Surfaces in
-- Settings → Privacy → "What did Yorik look up?" so the user can
-- audit what their LLM sent to / fetched from the open web.
--
-- Columns:
--   user_id     Which user triggered the call (per-user privacy view).
--   action      'search' or 'fetch'.
--   query       The SEARCH QUERY actually sent (post-PII-stripping).
--               NULL for fetch.
--   url         The URL fetched. NULL for search.
--   provider    'brave' | 'duckduckgo' | 'searxng (…)' | 'trafilatura'.
--   ok          1/0 — whether the call succeeded.
--   status      HTTP status code for fetch; null for search.
--   bytes       Response size (raw) for fetch; null for search.
--   error       Short error string when ok=0.
--   at          ISO timestamp.

CREATE TABLE IF NOT EXISTS web_visits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    action    TEXT NOT NULL,
    query     TEXT,
    url       TEXT,
    provider  TEXT,
    ok        INTEGER NOT NULL DEFAULT 1,
    status    INTEGER,
    bytes     INTEGER,
    error     TEXT,
    at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES user_profiles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_web_visits_user_at
    ON web_visits(user_id, at DESC);
