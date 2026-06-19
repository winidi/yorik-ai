-- Split user_profiles.name into first_name + last_name so letterheads,
-- salutations and recipient blocks have proper components to work with.
-- The existing `name` field stays as the display fallback (and is what
-- session lookups + everywhere-else code already reads).
--
-- Backfill: split the current `name` on the FIRST space. "Lena Hoffmann"
-- → ("Anna", "Schmidt"); "Cher" → ("Cher", ""). Users with a single
-- name keep last_name empty, which is honest about what we know.

ALTER TABLE user_profiles ADD COLUMN first_name TEXT;
ALTER TABLE user_profiles ADD COLUMN last_name  TEXT;

UPDATE user_profiles
SET first_name = CASE
        WHEN instr(name, ' ') > 0 THEN substr(name, 1, instr(name, ' ') - 1)
        ELSE name
    END,
    last_name = CASE
        WHEN instr(name, ' ') > 0 THEN substr(name, instr(name, ' ') + 1)
        ELSE ''
    END
WHERE first_name IS NULL;
