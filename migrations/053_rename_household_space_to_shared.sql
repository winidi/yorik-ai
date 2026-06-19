-- Rename the seeded shared space's user-visible name from "Household"
-- to "Shared". "Household" was family-coded and didn't fit business
-- workspaces; "Shared" works for both.
--
-- The slug stays 'household' — that's the stable identifier used by
-- ~10 code paths (paperless provisioning, calendar backfill,
-- add_user_to_household, etc.). Only the name flips.
--
-- Idempotent: only flips spaces that still carry the default name.
UPDATE spaces
SET name = 'Shared'
WHERE kind = 'shared' AND slug = 'household' AND name = 'Household';
