-- Migration 024: snooze on email messages
--
-- One nullable ISO datetime column. When set in the future, the
-- message disappears from the Inbox view until the time rolls past.
-- Surfaced via the new Snoozed sidebar shortcut so the user can
-- always find what they parked.
--
-- No background job needed — the list endpoint just filters
-- `snoozed_until IS NULL OR snoozed_until <= datetime('now')` for
-- the normal Inbox views, and the inverse for the Snoozed view.
-- That keeps the resurfacing latency = the next list refetch
-- (8s polling), which is plenty.

ALTER TABLE email_messages ADD COLUMN snoozed_until TEXT;

-- Index lets the "is this message currently snoozed?" check and the
-- Snoozed-view query both finish in one indexed hit even at 50K+
-- messages.
CREATE INDEX IF NOT EXISTS ix_email_msgs_snoozed
    ON email_messages (owner_user_id, snoozed_until);
