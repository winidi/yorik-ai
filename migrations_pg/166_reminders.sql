-- Reminders at a time: "sag mir in einer Stunde Bescheid". Until
-- 2026-09-26 the chat used notify, which pushes at once, and told the
-- person "in einer Stunde". backend/reminders.py fires due rows once a
-- minute into the notification bell (which pushes).
--
-- Additive only: one new table.

CREATE TABLE IF NOT EXISTS reminders (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    due_at      TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    fired_at    TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders (due_at) WHERE fired_at IS NULL AND cancelled_at IS NULL;
