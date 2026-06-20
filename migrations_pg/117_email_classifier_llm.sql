-- Phase X.2 — LLM-based email classification (opt-in per user).
--
-- The heuristic rules in email_classifier.classify() ship by default
-- (fast, no LLM call). When the user turns on LLM mode, every new
-- incoming mail is routed through Qwen via HOMEOS_LLM_BASE_URL to
-- get a sharper category. The backfill endpoint reclassifies the
-- existing inbox at the user's pace with a resumable progress row.

-- 1. Per-user preference + the version stamp the user is currently on.
--    Heuristic version is implicit ("h1"); LLM version includes the
--    model name so future model swaps (e.g. Qwen 3.7) re-trigger
--    backfill instead of silently treating old rows as up-to-date.
ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS classifier_mode TEXT NOT NULL DEFAULT 'heuristic';

-- 2. Per-message stamp so the backfill knows what's already current.
--    NULL means "never classified by this user's current mode" → eligible.
ALTER TABLE email_messages
  ADD COLUMN IF NOT EXISTS classifier_version TEXT;

-- 3. Backfill progress — one row per user. UPSERTed on start/tick.
--    Status machine: idle → running → done | error. Idempotent: a
--    second start while running short-circuits, never resets progress.
CREATE TABLE IF NOT EXISTS email_classifier_progress (
    user_id      UUID PRIMARY KEY REFERENCES user_profiles(id) ON DELETE CASCADE,
    total        INTEGER NOT NULL DEFAULT 0,
    done         INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'idle',
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    last_error   TEXT
);
