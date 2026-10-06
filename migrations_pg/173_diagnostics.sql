-- Diagnostics: opt-in, pseudonymised error reports and usage counts
-- (backend/diagnostics/, plan 2026-10-06 with Dirk). Nothing here leaves
-- the house by itself: the admin switches each tier on after setup, the
-- person whose conversation it is reviews every error report, and the
-- outbox only sends rows that were queued that way.
--
-- diag_pseudonyms: a person, address, phone, chat or file seen in a
-- report becomes a stable token (person_7) — the HMAC of the value with
-- the installation's secret; the value itself is never stored here.
-- diag_reports: drafts, the queue and what was sent (the "what Yorik
-- has sent" screen). diag_usage_daily: the counts of tier 1 and 2.
-- Additive only: three new tables.
CREATE TABLE IF NOT EXISTS diag_pseudonyms (
    kind        TEXT NOT NULL,
    hmac        TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, hmac)
);

CREATE TABLE IF NOT EXISTS diag_reports (
    id              UUID PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id         UUID,                      -- whose conversation; NULL for usage rows
    conversation_id TEXT,
    message_idx     INTEGER,
    kind            TEXT NOT NULL,             -- 'error' | 'usage_daily'
    trigger         TEXT,                      -- thumbs_down | report_problem | skill_failure | search_empty | exception | daily
    status          TEXT NOT NULL DEFAULT 'draft',   -- draft | queued | sent | declined | failed
    payload         JSONB NOT NULL,
    scrub_summary   JSONB,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    sent_at         TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    delete_token    TEXT
);
CREATE INDEX IF NOT EXISTS idx_diag_reports_status ON diag_reports (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_diag_reports_user ON diag_reports (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS diag_usage_daily (
    day     DATE NOT NULL,
    metric  TEXT NOT NULL,
    value   TEXT NOT NULL,                     -- already bucketed ('3-4', '100-500')
    PRIMARY KEY (day, metric)
);
