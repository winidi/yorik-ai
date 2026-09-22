-- Emergency access ("Zugriff auf alles"): an adult can see everything in
-- the household for a limited time, with a reason, after re-entering
-- their password; every other adult is told, and this table is the log
-- they can read. Decided with Dirk 2026-09-22 (see backend/emergency.py).
CREATE TABLE IF NOT EXISTS emergency_access (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    reason      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    ended_at    TEXT,
    created_at  TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS ix_emergency_access_user ON emergency_access (user_id, ended_at, expires_at);
