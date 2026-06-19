-- Persisted error log — every WARNING+ record from the logging
-- subsystem also lands here, so Settings → Health can show "what's
-- been breaking lately" without making the user grep journald.
--
-- Capped at 1000 rows by the SqliteErrorHandler's inline prune on
-- every write — older entries roll into the rotating yorik.log file
-- anyway, so this table is purely the recent-and-surfaced view.

CREATE TABLE IF NOT EXISTS error_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    level        TEXT NOT NULL,             -- WARNING | ERROR | CRITICAL
    logger       TEXT NOT NULL,             -- e.g. 'yorik.email_fetcher'
    message      TEXT NOT NULL,
    traceback    TEXT,                       -- null for non-exception warnings
    request_path TEXT                        -- when emitted from inside an HTTP handler
);

CREATE INDEX IF NOT EXISTS ix_error_log_ts_desc ON error_log (ts DESC);
CREATE INDEX IF NOT EXISTS ix_error_log_level   ON error_log (level);
