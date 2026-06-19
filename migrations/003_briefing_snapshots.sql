-- Per-day briefing snapshots. When the day ends, the snapshot worker
-- captures yesterday's `day-recap` briefing and persists it so the user
-- can later open /r/briefing → Yesterday → arrow back N days and read
-- what happened, even after the underlying data (incoming email, new
-- WhatsApp messages categorized later, etc.) has drifted.
--
-- payload_json is the full briefing result dict — same shape as
-- /api/briefings/{id}/run returns, so the frontend can render a
-- snapshot identically to a live run.
--
-- (template_id, target_date) is unique so re-running the snapshot is
-- idempotent — the worker can safely re-fire without producing
-- duplicates.

CREATE TABLE IF NOT EXISTS briefing_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id   TEXT NOT NULL,            -- e.g. 'day-recap'
    target_date   TEXT NOT NULL,            -- YYYY-MM-DD the briefing covers
    payload_json  TEXT NOT NULL,            -- full briefing result
    generated_at  TEXT NOT NULL,            -- when this snapshot was taken
    UNIQUE (template_id, target_date)
);

CREATE INDEX IF NOT EXISTS ix_briefing_snapshots_date_desc
    ON briefing_snapshots (target_date DESC);
