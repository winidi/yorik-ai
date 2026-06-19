-- Trusted-kiosk device registry.
--
-- Lets a household admin mark "this physical tablet is a kiosk
-- wall, remember it forever" so the per-login dance of going to
-- Settings → Devices and picking an album every time the user
-- re-authenticates goes away.
--
-- The device_id is the UUID the yorik-wall Android wrapper
-- generates on first install and forwards to the server in the
-- X-Yorik-Wall-Device header on every API call. On auth/login
-- (auth_sessions.create_session), if the incoming request carries
-- a header that matches a row in this table, the new session is
-- auto-flagged is_kiosk with the saved album / show_today /
-- hotword / block_phrases. From the user's POV: install, log in
-- once, mark trusted, done; future logins (or PWA cookie wipes)
-- inherit the kiosk role automatically.
--
-- user_id ties trust to the admin who set it up; revoking the
-- admin's account should orphan the trust (CASCADE not used so
-- it stays visible for audit until someone explicitly cleans up).
--
-- The kiosk fields mirror the columns on `sessions` so applying
-- the policy is a straight per-column copy.

CREATE TABLE trusted_kiosk_devices (
    device_id              TEXT PRIMARY KEY,    -- UUID from the wrapper
    user_id                INTEGER NOT NULL,    -- admin who trusted it
    device_label           TEXT,                -- "Living Room Tablet" etc.
    kiosk_album_id         TEXT,
    kiosk_show_today       INTEGER NOT NULL DEFAULT 0,
    kiosk_hotword_enabled  INTEGER NOT NULL DEFAULT 0,
    kiosk_block_phrases    TEXT,                -- JSON array
    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Lookup by user (admin's "Devices" page lists what they've trusted)
CREATE INDEX ix_trusted_kiosk_devices_user ON trusted_kiosk_devices(user_id);
