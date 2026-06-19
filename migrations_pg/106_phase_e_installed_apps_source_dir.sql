-- Phase E §6/§13 — record an app's source directory so the loader can
-- re-register v2 installs at boot.
--
-- Without this, in-place installs (install_app_from_dir, no copy)
-- vanish from /api/apps after a systemctl restart even though the
-- Postgres schema and ledger row are intact. scan_and_load_all only
-- walks apps/ on disk; the ledger knows what's installed but not
-- where to read connector.py + app.js from.

ALTER TABLE installed_apps
    ADD COLUMN IF NOT EXISTS source_dir TEXT;

COMMENT ON COLUMN installed_apps.source_dir IS
    'Absolute path to the app source directory (where manifest.json + connector.py + app.js live). Loader re-reads this on boot to re-register v2 apps without rescanning apps/.';
