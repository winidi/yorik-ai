-- Per-user PIN for kiosk-mode device user-switching.
--
-- Tablets mounted in shared spaces (living room kiosk, kitchen wall)
-- need a way for any household member to identify themselves WITHOUT
-- typing the full long password. Voice ID is the primary path; this
-- PIN is the fallback when voice-ID confidence is too low ("Yorik
-- thinks this is Sarah — tap to confirm" → wrong → "pick Dirk →
-- enter 4 digits").
--
-- bcrypt hash of a 4-digit (default; UI enforces) numeric PIN. NULL
-- when the user hasn't set one — kiosk fallback then just shows the
-- avatar grid without a PIN prompt for that user, which is fine on
-- a single-family kiosk but not for shared workspaces. Admins can
-- require PIN-per-user from Settings.
--
-- pin_set_at gives Settings a "PIN set on …" line + lets us prompt
-- for a refresh after a long time.
--
-- Both nullable so existing user_profiles rows are unaffected. The
-- kiosk fallback degrades gracefully (skips PIN step) when pin_hash
-- is NULL.

ALTER TABLE user_profiles ADD COLUMN pin_hash TEXT;
ALTER TABLE user_profiles ADD COLUMN pin_set_at TEXT;
