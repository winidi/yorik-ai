-- 030 — calendar colors that don't collide with event categories.
--
-- Why: the frontend tints shared events (events on someone else's
-- calendar overlayed onto your view) with the calendar's color so
-- "is this mine or hers" is glanceable. The original personal-cycle
-- palette from migration 010 (violet, blue, emerald, rose, yellow)
-- collides with the event-category palette (family=emerald, drive=
-- amber, health=rose, personal=violet, social=sky), so a shared event
-- can render in a color that the eye also reads as "this is a doctor's
-- appointment." The new palette is chosen to be visually distinct from
-- every category color.
--
-- Mapping (preserves user_id % 5 → palette slot from migration 010):
--   slot 0  #a78bfa (violet-400)  → #d946ef  (fuchsia-500)
--   slot 1  #60a5fa (blue-400)    → #6366f1  (indigo-500)
--   slot 2  #34d399 (emerald-400) → #84cc16  (lime-500)
--   slot 3  #fb7185 (rose-400)    → #f97316  (orange-500)
--   slot 4  #facc15 (yellow-400)  → #22d3ee  (cyan-400)
--   Shared  #f59e0b (amber-500)   → #ec4899  (pink-500)
--
-- Only touches calendars whose color still matches the migration-010
-- seed exactly — anything the user has manually re-skinned is left
-- alone.

UPDATE calendars
SET color = CASE owner_user_id % 5
    WHEN 0 THEN '#d946ef'
    WHEN 1 THEN '#6366f1'
    WHEN 2 THEN '#84cc16'
    WHEN 3 THEN '#f97316'
    ELSE        '#22d3ee'
END
WHERE kind = 'personal'
  AND color IN ('#a78bfa', '#60a5fa', '#34d399', '#fb7185', '#facc15');

UPDATE calendars
SET color = '#ec4899'
WHERE kind = 'shared'
  AND color = '#f59e0b';
