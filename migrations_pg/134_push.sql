-- Web Push: one row per browser/device that opted in, plus the two
-- daily nudges a user can switch on (morning: plan the day, evening:
-- review it). Times are local wall-clock HH:MM in YORIK_TZ.

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id           BIGSERIAL PRIMARY KEY,
    user_id      UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    endpoint     TEXT NOT NULL UNIQUE,
    p256dh       TEXT NOT NULL,
    auth         TEXT NOT NULL,
    user_agent   TEXT,
    created_at   TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    last_ok_at   TEXT,
    failures     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS push_subscriptions_user_idx ON push_subscriptions (user_id);

ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS nudge_morning TEXT;   -- e.g. '07:30', NULL = off
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS nudge_evening TEXT;   -- e.g. '18:30', NULL = off
