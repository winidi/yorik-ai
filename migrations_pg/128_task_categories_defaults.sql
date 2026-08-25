-- Default task categories.
--
-- The SQLite schema seeded these in init_db(); the Postgres path skipped
-- that step, so the category picker was empty on every fresh install.
INSERT INTO task_categories (name, color, position)
SELECT v.name, v.color, v.position
FROM (VALUES
    ('Home',     '#818cf8', 0),
    ('Work',     '#34d399', 1),
    ('Family',   '#fbbf24', 2),
    ('Shopping', '#60a5fa', 3),
    ('Health',   '#f87171', 4)
) AS v(name, color, position)
WHERE NOT EXISTS (SELECT 1 FROM task_categories t WHERE t.name = v.name);
