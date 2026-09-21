-- A child's school timetable for the family board: one document per
-- person, {"periods": [{"start": "08:00", "end": "08:45"}, ...],
-- "cells": {"<weekday 0-4>-<period index>": {"subject": "...", "room": "..."}}}.
CREATE TABLE IF NOT EXISTS timetables (
    user_id    UUID PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    updated_by UUID
);
