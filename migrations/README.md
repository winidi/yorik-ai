# Schema migrations

This directory holds versioned schema migrations. The runner is at
`backend/migrations.py`; it's invoked from `init_db()` at every
backend startup, so applying a migration is just "land a file in
this directory and restart Yorik (or hit `yorik db migrate`)".

## File naming

`NNN_short_description.{sql,py}`

- `NNN` — zero-padded version number, strictly monotonic, no gaps,
  no duplicates. `001`, `002`, ..., `042`. Three digits is enough
  for a couple of decades of weekly migrations.
- `short_description` — snake_case English, informational only.
  Pick something a `git log` reader can understand without opening
  the file. e.g. `add_priority_to_tasks`, `backfill_event_colors`.
- Extension — `.sql` for pure schema or simple data; `.py` for
  anything that needs Python (complex backfills, conditional
  transforms, calls into other modules).

Bad: `update.sql`, `001-add-thing.sql` (dashes break the regex),
`fix.py`, `1_thing.sql` (less than 3 digits).

## Format

### `.sql` migrations

The file is fed to `conn.executescript()`. You can put any number of
statements separated by `;`. The whole file runs inside one
transaction — if any statement fails, the transaction rolls back and
the migration is NOT marked as applied.

```sql
-- 042_add_priority_to_tasks.sql
ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 1;
CREATE INDEX ix_tasks_priority ON tasks(priority);
```

### `.py` migrations

The file is imported and its `up(conn)` function is called. The conn
is a `sqlite3.Connection`; everything inside `up()` runs in the same
transaction as the version-stamping, so a `raise` inside `up()`
rolls back the whole thing.

```python
# 043_backfill_priority_from_old_tags.py

def up(conn):
    """Read the old `tags` field, infer priority from any 'urgent'
    or 'high' tags, and write to the new `priority` column."""
    rows = conn.execute("SELECT id, tags FROM tasks").fetchall()
    for row in rows:
        tags = (row[1] or "").lower().split(",")
        p = 2 if "urgent" in tags or "high" in tags else 1
        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (p, row[0]))
```

## Idempotency + dev workflow

The runner skips migrations whose version is already in
`schema_migrations`. So a migration runs **at most once per database**.
This means:

- Don't edit a migration after it's been applied to any deployed box
  — the change won't re-run. Add a new migration instead.
- During local dev, if you need to re-test a migration, delete its
  row from `schema_migrations` (or wipe the test DB).

## Relationship to `_ensure_columns()`

The legacy `_ensure_columns()` helper in `backend/database.py` still
handles all schema changes made before this framework existed. Don't
remove those calls — they keep upgrade-from-old-installs working.
New schema changes from this commit forward should go into a
migration file. Eventually (years from now) the `_ensure_columns`
backlog can be retired once every supported install has crossed the
corresponding migration version.

## Operational

- `yorik db status` — list applied + pending migrations
- `yorik db migrate` — apply pending ones explicitly (also happens
  automatically on every backend startup)
- Tracking table:
  ```sql
  SELECT * FROM schema_migrations ORDER BY version;
  ```

## Failures

If a migration fails:
1. The transaction rolls back — the partial schema/data change is gone
2. `schema_migrations` is NOT updated — the migration is still "pending"
3. Backend startup continues but logs a loud error
4. Fix the migration file (or write a follow-up that handles the
   broken state), restart, and `run_pending()` will retry from the
   failing version
