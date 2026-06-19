-- Kiosk-mode session columns.
--
-- The existing `sessions` table already tracks user_id + expires_at +
-- last_seen_at for normal browser sessions. Kiosk mode adds a few
-- columns that DON'T touch any of the normal session flow:
--
--   is_kiosk:        marks a session as a wall-mounted tablet kiosk.
--                    The /api/ambient/* routes refuse non-kiosk
--                    sessions, so regular PC users never accidentally
--                    enter kiosk mode. Default 0 = normal.
--
--   kiosk_album_id:  the Immich album UUID the slideshow reads from.
--                    Per-device so the kids' room kiosk can show a
--                    kid-friendly album while the living room shows
--                    the family album. NULL when admin hasn't picked
--                    one yet — slideshow renders a "configure album
--                    in Settings →" link.
--
--   device_label:    "Living Room Tablet", "Kitchen Wall", etc.
--                    User-friendly name shown in Settings → Devices
--                    + on the kiosk's own setup screen so the user
--                    knows which device they're configuring.
--
--   trusted_until:   when set, this session has long-TTL trusted-
--                    device status. PIN-based user-switching is
--                    allowed on this session without a full password.
--                    Used for kiosk + (later) any device the user
--                    explicitly trusts. NULL on the normal 30-day
--                    sessions — they require full password to switch.
--
-- All columns are nullable / default-zero so existing rows keep
-- working unchanged. The /api/ambient/* + PIN-switch endpoints are
-- the only readers; everything else ignores these columns.

ALTER TABLE sessions ADD COLUMN is_kiosk        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN kiosk_album_id  TEXT;
ALTER TABLE sessions ADD COLUMN device_label    TEXT;
ALTER TABLE sessions ADD COLUMN trusted_until   TEXT;

-- Partial index — kiosk sessions are a tiny minority and the route
-- handlers filter on is_kiosk=1 for slideshow/idle. Saves index
-- bytes on the common case.
CREATE INDEX IF NOT EXISTS ix_sessions_kiosk
    ON sessions(is_kiosk)
    WHERE is_kiosk = 1;
