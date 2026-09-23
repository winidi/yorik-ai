-- Mail sync that loses nothing (2026-09-23).
--
-- 1. A mail that fails to parse or to save is recorded here instead of
--    being skipped for good. The fetcher retries it on every pass and
--    parks it after a few attempts; parked ones are shown per account.
CREATE TABLE IF NOT EXISTS email_fetch_failures (
    account_id      BIGINT  NOT NULL,
    folder_id       BIGINT  NOT NULL,
    uid             BIGINT  NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 1,
    last_error      TEXT,
    first_failed_at TEXT    NOT NULL DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
    last_tried_at   TEXT    NOT NULL DEFAULT (to_char(now(), 'YYYY-MM-DD HH24:MI:SS')),
    parked          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, folder_id, uid)
);
CREATE INDEX IF NOT EXISTS ix_email_fetch_failures_account ON email_fetch_failures (account_id, parked);

-- 2. How far back an account is kept in Yorik: 'recent' (the latest 200
--    mails in the inbox, 50 in every other folder), 'days:N', or 'all'.
--    The sync pass keeps everything on the server inside that scope in
--    Yorik, and fetches whatever is missing.
ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS import_scope TEXT NOT NULL DEFAULT 'recent';
-- What the sync pass is doing, for the settings screen (JSON:
-- phase, done, total, missing, removed, last_run_at).
ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS sync_state TEXT;
-- Set by "Repair now": the next pass compares every folder in full.
ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS repair_requested INTEGER NOT NULL DEFAULT 0;

ALTER TABLE email_folders ADD COLUMN IF NOT EXISTS last_reconcile_at TEXT;
