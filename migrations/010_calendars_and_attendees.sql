-- Calendar overlay + invitations.
--
-- Adds the "calendar is a collection" model on top of the flat events
-- table. Each user gets a private Personal calendar plus access to a
-- household-wide Shared calendar. Per-calendar ACL (free_busy | read |
-- write) governs cross-user visibility. The `event_attendees` table
-- powers invitation + RSVP + propose-new-time across users.
--
-- See backend/calendars.py for the can_access() helper that resolves
-- (user, calendar, requested_level) → boolean using these tables.

-- ── Calendars ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS calendars (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    -- Hex like '#a78bfa'. UI uses this for the chip color and the
    -- sidebar eye-toggle dot.
    color           TEXT NOT NULL DEFAULT '#a78bfa',
    owner_user_id   INTEGER NOT NULL,
    -- 'personal'  → auto-created on user signup, owned by that user
    -- 'shared'    → household / business default, write-shared with all
    -- 'project'   → user-created arbitrary calendar
    kind            TEXT NOT NULL DEFAULT 'personal',
    -- When 1, even admin role cannot read events on this calendar
    -- unless explicitly shared. Off by default — admin sees all so the
    -- household head / business owner has the full picture.
    hide_from_admin INTEGER NOT NULL DEFAULT 0,
    -- Soft delete so historical events don't break.
    archived_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (owner_user_id) REFERENCES user_profiles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_calendars_owner ON calendars(owner_user_id);

-- ── Calendar shares (ACL) ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS calendar_shares (
    calendar_id     INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    -- 'free_busy' → only see time blocks, no titles
    -- 'read'      → see all event details
    -- 'write'     → read + create/edit/delete events on this calendar
    access_level    TEXT NOT NULL DEFAULT 'free_busy',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (calendar_id, user_id),
    FOREIGN KEY (calendar_id) REFERENCES calendars(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id)     REFERENCES user_profiles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_calendar_shares_user ON calendar_shares(user_id);

-- ── Events get calendar_id + visibility + ownership ───────────────────
-- The existing `allowed_roles` column stays for backward compat with
-- legacy /api/events?role=… filtering, but the calendar's ACL becomes
-- load-bearing going forward.

ALTER TABLE events ADD COLUMN calendar_id     INTEGER;
ALTER TABLE events ADD COLUMN owner_user_id   INTEGER;
-- 'default' → uses the calendar's ACL as-is
-- 'private' → even on shared calendars, hide title+notes from anyone
--             except the owner; they see opaque "Busy"
ALTER TABLE events ADD COLUMN visibility      TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS ix_events_calendar ON events(calendar_id);
CREATE INDEX IF NOT EXISTS ix_events_owner    ON events(owner_user_id);

-- ── Event attendees (invitation model) ────────────────────────────────
-- Each row either references a user_id (logged-in account; RSVP applies)
-- OR holds a free-text person_name (kid without a login, external
-- contact, etc.; no RSVP gate, just shown on the event chip).

CREATE TABLE IF NOT EXISTS event_attendees (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id            INTEGER NOT NULL,
    user_id             INTEGER,          -- nullable when person_name is set
    person_name         TEXT,             -- nullable when user_id is set
    -- 'needs_action' default until the user RSVPs.
    -- 'accepted' / 'declined' / 'tentative' are the three responses.
    -- Non-user attendees (person_name) stay 'needs_action' forever — UI
    -- treats them as informational.
    response_status     TEXT NOT NULL DEFAULT 'needs_action',
    -- Counter-proposal time the attendee suggested instead of the
    -- event's start. Single counter, no thread — keeps the flow simple.
    proposed_time_iso   TEXT,
    response_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id)  REFERENCES user_profiles(id) ON DELETE CASCADE,
    -- Guard: exactly one of (user_id, person_name) must be set.
    CHECK ((user_id IS NULL) <> (person_name IS NULL))
);

CREATE INDEX IF NOT EXISTS ix_event_attendees_event ON event_attendees(event_id);
CREATE INDEX IF NOT EXISTS ix_event_attendees_user  ON event_attendees(user_id);

-- ── Seed: one Shared calendar + a personal calendar per user ──────────
-- Default owner is the first admin we find. Migrates every existing
-- event into the Shared calendar so the day-1 view is unchanged.
-- Personal calendars start empty.

INSERT INTO calendars (name, color, owner_user_id, kind)
SELECT 'Shared', '#f59e0b',
       (SELECT id FROM user_profiles WHERE role = 'admin' ORDER BY id LIMIT 1),
       'shared'
WHERE EXISTS (SELECT 1 FROM user_profiles WHERE role = 'admin')
  AND NOT EXISTS (SELECT 1 FROM calendars WHERE kind = 'shared');

-- Write-share the Shared calendar to every user.
INSERT INTO calendar_shares (calendar_id, user_id, access_level)
SELECT c.id, u.id, 'write'
FROM calendars c, user_profiles u
WHERE c.kind = 'shared'
  AND u.id <> c.owner_user_id
  AND NOT EXISTS (
    SELECT 1 FROM calendar_shares s
    WHERE s.calendar_id = c.id AND s.user_id = u.id
  );

-- One personal calendar per user (skip those who already have one — keeps
-- the migration idempotent so re-running is safe).
INSERT INTO calendars (name, color, owner_user_id, kind)
SELECT COALESCE(u.name, 'My') || '''s calendar',
       -- Cycle a few pleasant colors so personal calendars are visually
       -- distinguishable in the overlay view.
       CASE (u.id % 5)
         WHEN 0 THEN '#a78bfa'  -- violet
         WHEN 1 THEN '#60a5fa'  -- blue
         WHEN 2 THEN '#34d399'  -- emerald
         WHEN 3 THEN '#fb7185'  -- rose
         ELSE        '#facc15'  -- yellow
       END,
       u.id, 'personal'
FROM user_profiles u
WHERE NOT EXISTS (
    SELECT 1 FROM calendars c
    WHERE c.owner_user_id = u.id AND c.kind = 'personal'
);

-- Backfill: existing events with no calendar → into Shared.
UPDATE events
SET calendar_id = (SELECT id FROM calendars WHERE kind = 'shared' LIMIT 1)
WHERE calendar_id IS NULL;
