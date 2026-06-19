-- Rename the seeded "Shared" calendar to "Household".
--
-- The original migration 010 named the household-wide bucket "Shared",
-- which collided with the broader UI concept of "shared view" (an
-- aggregate overlay of everyone's calendars). "Household" makes it
-- clear the bucket is specifically for person-agnostic items (trash
-- day, mortgage, holidays) rather than the meta-view of everyone's
-- events. Business installs will rename it further ("Office", "Team",
-- whatever) via /api/calendars PATCH.
--
-- Idempotent: only renames when both kind matches AND the name is
-- still the migration-010 default. If the user has already manually
-- renamed it, we leave their choice alone.

UPDATE calendars
SET name = 'Household'
WHERE kind = 'shared' AND name = 'Shared';
