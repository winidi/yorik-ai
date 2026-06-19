"""Demo / example data — one-shot seed for first-run "I want to see
what Yorik feels like with stuff in it" exploration.

Design notes
------------

- All dates are computed **relative to today** so the demo always feels
  current. If we shipped fixed 2026 dates, by 2027 the "Today" bucket
  in the Tasks app would be empty and the demo would look broken.
- Every row we seed gets its ID recorded in `app_settings` under
  `demo_seed_ids` (JSON: `{table: [id, ...]}`). Removal walks that
  manifest and deletes precisely what we added — no LIKE-matching on
  titles, no hope.
- Only ADMIN should be able to seed/remove (it mutates shared tables).
- Idempotency: calling `seed_all` twice without removal in between is
  a programming error — caller is expected to check `is_seeded()` and
  skip / remove first. The endpoint enforces this; this module just
  appends to whatever manifest exists.
- Bills + tasks intentionally include a "due in 3 days" entry so the
  morning briefing card has something to show immediately.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from .database import conn_ctx, DEFAULT_DB_PATH

log = logging.getLogger("yorik.demo_data")

_MANIFEST_KEY = "demo_seed_ids"
_TIMESTAMP_KEY = "demo_seeded_at"


# ─── Static demo content (will be date-shifted at seed time) ─────────

# Each event uses `day_offset` relative to today + a HH:MM time.
# Negative offsets = recently past (visible in week view), positive = upcoming.
_DEMO_EVENTS = [
    # (day_offset, start_hhmm, duration_min, title, person, color, all_day)
    (-1, "09:00",  60, "Sprint planning",            "admin",  "#3b82f6", 0),
    (-1, "13:30",  90, "Mittagessen mit Anna",       "admin",  "#a78bfa", 0),
    ( 0, "10:00",  30, "Zahnarzt",                   "admin",  "#f59e0b", 0),
    ( 0, "16:00",  60, "Sport (laufen)",             "admin",  "#10b981", 0),
    ( 1, "14:00",  90, "Demo: Kunde Müller GmbH",    "admin",  "#ec4899", 0),
    ( 2, "00:00",   0, "Schulfest",                  "child",  "#a78bfa", 1),
    ( 3, "19:30", 150, "Konzert Tempodrom",          "admin",  "#8b5cf6", 0),
    ( 6, "12:00",  60, "Brunch bei den Eltern",      "admin",  "#a78bfa", 0),
]

# Tasks: (day_offset_due_or_None, title, priority, est_min, category)
_DEMO_TASKS = [
    ( 0, "Rechnung Müller GmbH erstellen",       2, 45, "Arbeit"),
    ( 0, "Spülmaschine reparieren lassen",       1, 15, "Haushalt"),
    ( 1, "Steuerunterlagen für 2025 sortieren",  1, 120, "Steuer"),
    ( 2, "Stromzähler ablesen",                  0,  5, "Haushalt"),
    ( 3, "Geburtstagsgeschenk für Oma kaufen",   1, 30, "Familie"),
    ( 7, "Quartalsbericht Q2 fertigstellen",     2, 180, "Arbeit"),
    (None, "Backup-Strategie überprüfen",        0, 30, "Wartung"),
]

# Bills: (day_offset_due, name, amount, currency)
_DEMO_BILLS = [
    ( 3, "Strom — Vattenfall",         98.50,  "EUR"),
    ( 8, "Internet — Telekom",         49.99,  "EUR"),
    (15, "Versicherung — Allianz",    245.00,  "EUR"),
]

# Notifications: nudges that show off the bell + briefing card.
_DEMO_NOTIFICATIONS = [
    {
        "kind": "demo_welcome",
        "title": "Willkommen bei Yorik 👋",
        "body":  "Das ist Demo-Daten — du kannst sie unter Settings → Demo wieder löschen, sobald du genug gesehen hast.",
        "navigate_to": "/r/home",
    },
]


# ─── Manifest helpers ────────────────────────────────────────────────

def _load_manifest(conn) -> Dict[str, List[int]]:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (_MANIFEST_KEY,),
    ).fetchone()
    if not row or not row["value"]:
        return {}
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        log.warning("demo_data: manifest unparseable, treating as empty")
        return {}


def _save_manifest(conn, manifest: Dict[str, List[int]]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
        "VALUES (?, ?, datetime('now'))",
        (_MANIFEST_KEY, json.dumps(manifest, ensure_ascii=False)),
    )


def _stamp_seeded_at(conn) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
        "VALUES (?, ?, datetime('now'))",
        (_TIMESTAMP_KEY, datetime.now().isoformat(timespec="seconds")),
    )


def _clear_stamp(conn) -> None:
    conn.execute("DELETE FROM app_settings WHERE key IN (?, ?)",
                 (_MANIFEST_KEY, _TIMESTAMP_KEY))


# ─── Public API ──────────────────────────────────────────────────────

def is_seeded() -> bool:
    """True if any demo data is currently installed (per the manifest)."""
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        m = _load_manifest(conn)
        return any(ids for ids in m.values())


def summary() -> Dict[str, Any]:
    """Lightweight status: per-table counts + when it was seeded."""
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        m = _load_manifest(conn)
        ts_row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_TIMESTAMP_KEY,),
        ).fetchone()
    return {
        "seeded":     any(ids for ids in m.values()),
        "seeded_at":  (ts_row["value"] if ts_row else None),
        "counts":     {table: len(ids) for table, ids in m.items()},
        "total":      sum(len(ids) for ids in m.values()),
    }


def seed_all(user_id: str = 1) -> Dict[str, Any]:
    """Insert all demo content. Returns the manifest of what was added.
    Caller MUST check is_seeded() first — re-seeding without removal
    duplicates the data."""
    today = date.today()
    inserted: Dict[str, List[int]] = {"events": [], "tasks": [], "bills": [], "notifications": []}

    with conn_ctx(DEFAULT_DB_PATH) as conn:
        # Events
        for offset, hhmm, dur, title, person, color, all_day in _DEMO_EVENTS:
            event_date = today + timedelta(days=offset)
            if all_day:
                starts = f"{event_date.isoformat()}T00:00:00"
                ends   = f"{event_date.isoformat()}T23:59:59"
            else:
                h, m = map(int, hhmm.split(":"))
                start_dt = datetime.combine(event_date, datetime.min.time()).replace(hour=h, minute=m)
                starts = start_dt.isoformat(timespec="seconds")
                ends   = (start_dt + timedelta(minutes=dur)).isoformat(timespec="seconds")
            # Phase B: events live on the Shared (Household) calendar by
            # default so the demo "Family dinner" etc. show up in everyone's
            # view. Caller can override by editing post-seed.
            shared_cal = conn.execute(
                "SELECT c.id FROM calendars c JOIN spaces s ON s.id=c.space_id "
                "WHERE s.slug='household' AND c.kind='shared' LIMIT 1"
            ).fetchone()
            cur = conn.execute(
                "INSERT INTO events (title, starts_at, ends_at, all_day, color, person, calendar_id, owner_user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (title, starts, ends, all_day, color, person,
                 shared_cal["id"] if shared_cal else None, user_id),
            )
            inserted["events"].append(cur.lastrowid)

        # Tasks → user's personal space; Bills → Finance.
        personal_row = conn.execute(
            "SELECT id FROM spaces WHERE kind='personal' AND owner_user_id=?", (user_id,)
        ).fetchone()
        personal_space = personal_row["id"] if personal_row else None
        finance_row = conn.execute(
            "SELECT id FROM spaces WHERE slug='finance' LIMIT 1"
        ).fetchone()
        finance_space = finance_row["id"] if finance_row else None

        for offset, title, priority, est_min, category in _DEMO_TASKS:
            due = (today + timedelta(days=offset)).isoformat() if offset is not None else None
            cur = conn.execute(
                "INSERT INTO tasks (title, due_date, done, category, priority, estimated_minutes, space_id, created_by_user_id) "
                "VALUES (?, ?, 0, ?, ?, ?, ?, ?)",
                (title, due, category, priority, est_min, personal_space, user_id),
            )
            inserted["tasks"].append(cur.lastrowid)

        # Bills
        for offset, name, amount, currency in _DEMO_BILLS:
            due = (today + timedelta(days=offset)).isoformat()
            cur = conn.execute(
                "INSERT INTO bills (name, amount, currency, due_date, space_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, amount, currency, due, finance_space),
            )
            inserted["bills"].append(cur.lastrowid)

        # Notifications
        for n in _DEMO_NOTIFICATIONS:
            cur = conn.execute(
                "INSERT INTO notifications (user_id, kind, title, body, navigate_to) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, n["kind"], n["title"], n["body"], n.get("navigate_to")),
            )
            inserted["notifications"].append(cur.lastrowid)

        _save_manifest(conn, inserted)
        _stamp_seeded_at(conn)
        conn.commit()

    log.info("demo_data: seeded %d items: %s",
             sum(len(ids) for ids in inserted.values()),
             {k: len(v) for k, v in inserted.items()})
    return inserted


def remove_all() -> Dict[str, int]:
    """Delete every entry recorded in the manifest. Returns per-table
    deletion counts. Silently ignores already-deleted rows (user might
    have manually removed one)."""
    deleted: Dict[str, int] = {}
    with conn_ctx(DEFAULT_DB_PATH) as conn:
        m = _load_manifest(conn)
        if not m:
            return {}
        for table, ids in m.items():
            if not ids:
                continue
            placeholders = ",".join("?" * len(ids))
            cur = conn.execute(
                f"DELETE FROM {table} WHERE id IN ({placeholders})", ids,
            )
            deleted[table] = cur.rowcount or 0
        _clear_stamp(conn)
        conn.commit()
    log.info("demo_data: removed %d items: %s",
             sum(deleted.values()), deleted)
    return deleted
