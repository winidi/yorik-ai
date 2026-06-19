-- Yorik Phase E §6 — installed_apps ledger + per-app schema lifecycle helpers.
--
-- One row per community-app install. The manifest snapshot, the
-- permissions granted at install time, who granted, and when. This
-- is what the consent screen (§7) writes after the user clicks
-- "Install". The uninstall step (§6) sets uninstalled_at and leaves
-- the row for audit.

CREATE TABLE IF NOT EXISTS installed_apps (
    id                  BIGSERIAL PRIMARY KEY,
    app_id              TEXT NOT NULL,                -- manifest.id (e.g. "acme.notes")
    version             TEXT NOT NULL,                -- manifest.version
    owned_schema        TEXT NOT NULL,                -- Postgres schema name (e.g. "app_notes")
    manifest_snapshot   JSONB NOT NULL,               -- full manifest at install time
    granted_permissions JSONB NOT NULL,               -- the permissions block (subset of manifest)
    granted_by_user_id  UUID REFERENCES user_profiles(id) ON DELETE SET NULL,
    granted_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    uninstalled_at      TIMESTAMPTZ,
    -- App-specific role + JWT (for §6.6 — apps get their own
    -- service-role-like JWT scoped to their schema). Filled in when
    -- we wire up the per-app role; nullable until then so partial
    -- rollouts don't break.
    app_role_name       TEXT,
    app_jwt_encrypted   TEXT,
    notes               TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS installed_apps_one_active_per_id
    ON installed_apps (app_id)
    WHERE uninstalled_at IS NULL;

CREATE INDEX IF NOT EXISTS installed_apps_granted_by_idx
    ON installed_apps (granted_by_user_id);

COMMENT ON TABLE installed_apps IS
    'Yorik Phase E §6 — ledger of community-app installs. One active row per app_id (enforced by partial unique index). Uninstall sets uninstalled_at but keeps the row for audit.';
