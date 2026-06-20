-- Phase Y.0.a — LLM-assisted triage of pending contacts.
--
-- The existing TriageModal is a fast manual approve/dismiss surface
-- (status pending → active or spam). For installs with hundreds of
-- pending rows it's still tedious because the user is the classifier.
-- This migration adds three columns that an LLM pre-pass populates,
-- so the modal can OPEN with decisions already filled in. The user's
-- job becomes "scroll, confirm, override the weird ones, apply" —
-- minutes, not tens of minutes.
--
-- Outcome space is broader than the legacy approve/dismiss split:
--   active_person   — real human, individual relationship
--   active_business — real org with engagement signals (account refs,
--                     invoices, two-way correspondence)
--   archived        — pure outbound marketing / one-way blasts;
--                     gentler than spam, no sender block
--   spam            — unsolicited / aggressive / fraudulent;
--                     same end-state as the existing dismiss
--
-- Verdict is just a SUGGESTION until the user clicks Apply.
-- triage_apply (the existing endpoint, soon to be extended) is the
-- only thing that actually flips contacts.status.
ALTER TABLE contacts
  ADD COLUMN IF NOT EXISTS triage_verdict     TEXT,
  ADD COLUMN IF NOT EXISTS triage_reason      TEXT,
  ADD COLUMN IF NOT EXISTS triage_confidence  TEXT,
  ADD COLUMN IF NOT EXISTS triage_classified_at TEXT;

-- Per-user progress row for the background LLM pass — same pattern
-- the email classifier backfill uses. owner_user_id NULLable for
-- single-tenant installs that don't set it.
CREATE TABLE IF NOT EXISTS contacts_triage_progress (
    owner_user_id UUID PRIMARY KEY REFERENCES user_profiles(id) ON DELETE CASCADE,
    total         INTEGER NOT NULL DEFAULT 0,
    done          INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'idle',
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    last_error    TEXT
);
