-- Per-channel upstream-provided name. Lets every modality (WhatsApp,
-- email, future Telegram/Signal/etc) store its own name for a
-- specific channel value WITHOUT competing for the single
-- contacts.display_name field.
--
-- UI precedence stays:
--   1. contacts.display_name  (user-set; never auto-overwritten when
--                              the user has set a real name)
--   2. first non-null contact_channels.display_name across channels
--      (modality preference enforced in the frontend)
--   3. the channel value, formatted for humans
--
-- Adding Telegram or Signal in the future: zero schema change. The
-- new autocapture path writes contact_channels(kind='telegram',
-- value=..., display_name='@whatever') and the precedence chain
-- picks it up automatically.
ALTER TABLE contact_channels
  ADD COLUMN IF NOT EXISTS display_name TEXT;
