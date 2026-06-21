-- Global suggestion-engine settings per user. Two toggles:
--
--   suggestions_enabled  master switch. OFF by default — the user
--                        has to deliberately turn AI suggestions on.
--                        When false, analyse_message short-circuits
--                        immediately. No retriever runs, no LLM call.
--
--   suggestion_sources   per-modality toggle. JSONB so adding
--                        WhatsApp/Telegram/etc. = zero schema change,
--                        just a new key. MVP defaults to email-only
--                        when the master is flipped on.
--
-- Confidence threshold (low/medium/high) and max-per-message belong
-- here too eventually; deferred to Day 5 polish.
ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS suggestions_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS suggestion_sources  JSONB NOT NULL DEFAULT '{"email": true}'::jsonb;
