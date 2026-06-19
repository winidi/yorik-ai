#!/usr/bin/env python3
"""Seed a single populated week into the calendar — demo-screenshot fodder.

Companion to `seed-demo-data.py`. That script seeds 23 events spread over
±4 months, which leaves any given week sparse. This one drops ~20 events
on a single Mon–Sun so the weekly calendar grid looks alive in a
screenshot.

Defaults to THIS week (Monday of today's calendar week) so a screenshot
of the default calendar landing view isn't empty. Pass
`--anchor YYYY-MM-DD` to pick a different week (it snaps to the Monday
of whichever week that date is in); pass `--weeks N` to seed N
consecutive weeks starting from the anchor.

Every row carries the marker `[YORIK_DEMO_WEEK v1]` in its notes, so
removing the whole set later is one DELETE:

    sqlite3 data/family.db \\
      "DELETE FROM events WHERE notes LIKE '%[YORIK_DEMO_WEEK v1]%';"

Idempotent: re-running is a no-op once the rows exist.

Usage
-----
    # from project root with the venv active:
    python scripts/seed-demo-week.py                       # owner=1, this week
    python scripts/seed-demo-week.py --weeks 2             # this + next week
    python scripts/seed-demo-week.py --owner 2
    python scripts/seed-demo-week.py --anchor 2026-06-08

Names: fictional placeholders only ("ExampleCo", "Anna", "Mark") —
matches the no-real-names rule in seed-demo-data.py.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

MARKER = "[YORIK_DEMO_WEEK v1]"


def resolve_db_path() -> Path:
    raw = os.environ.get("HOMEOS_DB_PATH", "data/family.db")
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


def monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())


def this_week_monday(today: date) -> date:
    # Snap to Monday of whatever week today is in. Friday → previous
    # Monday, Sunday → previous Monday. Matches the default landing
    # view of /r/calendar which centres on the current week.
    return monday_of_week(today)


# weekday offsets from Monday: 0=Mon, 1=Tue, ..., 6=Sun
def E(weekday: int, hh: int, mm: int, duration_h: float,
      title: str, *, category: str = "personal",
      person: str | None = None, location: str | None = None,
      notes: str = "",
      travel_min: int | None = None, travel_km: float | None = None) -> dict:
    """Create a seeded event row.

    `travel_min` + `travel_km`: when both are set AND a location is
    set, the script writes them into travel_time_s / travel_distance_m
    (with provider='ors' + computed_at=now) so the calendar's travel-
    time badge renders without needing the maps connector configured.
    Otherwise the event lands without travel data — same as a real
    event whose location couldn't be geocoded.
    """
    full_notes = (notes + " " + MARKER).strip()
    return {
        "weekday":   weekday,
        "hh":        hh,
        "mm":        mm,
        "duration_h": duration_h,
        "title":     title,
        "category":  category,
        "person":    person,
        "location":  location,
        "notes":     full_notes,
        "travel_min": travel_min,
        "travel_km":  travel_km,
    }


# Realism over filler: the calendar splits into things that genuinely
# recur every week (kids' lessons, gym routines, weekly standups) and
# things that don't (one-off client calls, doctor appointments,
# birthdays, day trips). Repeating the latter on every week makes the
# grid read as fake — no real person has the same birthday and the
# same client meeting two Mondays in a row.

# Things that genuinely recur every week. Same time, same name across
# however many weeks the user seeds. Mostly kids' lessons + personal
# routine + the standard work cadence (standup, weekly planning).
WEEKLY_ROUTINES: list[dict] = [
    E(0,  9, 0,  0.5, "Team standup",          category="business",
      person="Team"),
    E(0, 18, 0,  1.0, "Run training",          category="personal",
      location="City Park",
      travel_min=8, travel_km=2.4),
    E(1, 17, 30, 1.0, "Swim class — kids",     category="family",
      location="Community pool",
      travel_min=12, travel_km=4.1),
    E(2, 16, 0,  1.0, "Piano lesson",          category="family",
      person="Daughter"),
    E(2, 20, 0,  2.0, "Book club",             category="personal",
      location="Community center",
      travel_min=10, travel_km=3.6),
    E(4,  9, 0,  1.0, "Weekly planning",       category="business"),
    E(5, 10, 0,  2.0, "Farmers' market",       category="family",
      location="Town square",
      travel_min=6, travel_km=1.9),
    E(6, 10, 0,  1.0, "Yoga",                  category="personal",
      location="Community center",
      travel_min=10, travel_km=3.6),
]

# Per-week one-off events. The script cycles through this list — week
# 1 uses index 0, week 2 uses index 1, week N>len uses N % len(...).
# Each variant should net 10–13 events so total per week lands around
# 18–21 once WEEKLY_ROUTINES are merged.
WEEK_VARIANTS: list[list[dict]] = [
    # Variant A — "client week", health checkup, quarterly review,
    # parents' dinner, friend's birthday, family day trip.
    [
        E(0, 11, 0,  1.5, "Client meeting — ExampleCo", category="business",
          location="14 Example St, Springfield",
          travel_min=18, travel_km=9.2),
        E(1,  9, 30, 2.5, "Prep proposal",          category="business",
          notes="Deep-work block — door sign up."),
        E(1, 14, 0,  1.0, "GP — annual checkup",    category="health",
          location="22 Main St, Springfield",
          travel_min=12, travel_km=4.8),
        E(2,  8, 30, 1.0, "Accountant",             category="business",
          location="Example & Partners, Springfield",
          travel_min=15, travel_km=6.7),
        E(2, 12, 0,  1.5, "Lunch with Anna",        category="social",
          person="Anna",
          location="Café Example, Springfield",
          travel_min=8, travel_km=2.9),
        E(3,  9, 0,  1.5, "Quarterly review — Q2", category="business",
          person="Team"),
        E(3, 13, 0,  1.0, "Garage — car inspection", category="drive",
          location="Example Auto Service, Springfield",
          travel_min=14, travel_km=6.1),
        E(3, 18, 30, 2.0, "Dinner at parents'",     category="family",
          person="Mom, Dad",
          location="7 Example Ave, Hometown",
          travel_min=75, travel_km=92.4),
        E(4, 11, 0,  1.5, "Invoice run — May",      category="business",
          notes="Outstanding receivables + reminders."),
        E(4, 19, 0,  3.0, "Mark's birthday",        category="social",
          person="Mark",
          location="Restaurant Example, Springfield",
          travel_min=11, travel_km=4.5),
        E(5, 14, 0,  3.0, "Zoo with family",        category="family",
          location="City Zoo",
          travel_min=22, travel_km=12.1),
        E(6, 12, 0,  3.0, "Family brunch",          category="family"),
    ],

    # Variant B — "kickoff week", dentist instead of GP, strategy
    # workshop instead of quarterly review, cinema instead of birthday,
    # hike instead of zoo. Everything one-off is swapped.
    [
        E(0, 10, 0,  2.0, "Kickoff — migration project", category="business",
          person="Team",
          notes="New customer requirement — scope it out."),
        E(1,  9, 30, 2.0, "Supplier call",          category="business",
          location="Online — video call"),  # online = no travel
        E(1, 16, 0,  1.0, "Dentist checkup",        category="health",
          person="Dr. Example",
          location="18 Station Rd, Springfield",
          travel_min=9, travel_km=3.2),
        E(2,  9, 0,  3.0, "Strategy workshop",      category="business",
          person="Team",
          location="Office",
          travel_min=25, travel_km=14.6),
        E(2, 12, 30, 1.5, "Lunch with supplier",    category="social",
          person="ExampleCo sales",
          location="Restaurant Example, Springfield",
          travel_min=11, travel_km=4.5),
        E(3, 10, 0,  2.0, "Hiring interview",       category="business",
          notes="Frontend intern — first round."),
        E(3, 14, 0,  1.0, "Pilates",                category="personal",
          location="Studio Example",
          travel_min=7, travel_km=2.1),
        E(3, 19, 0,  2.0, "Club meeting",           category="social",
          location="Clubhouse Springfield",
          travel_min=13, travel_km=5.5),
        E(4, 14, 0,  2.0, "Workshop — website refresh", category="business",
          notes="With the agency — website update."),
        E(4, 20, 0,  2.5, "Cinema night",           category="social",
          person="Anna",
          location="Cinema Springfield",
          travel_min=14, travel_km=6.2),
        E(5, 11, 0,  4.0, "Day hike",               category="family",
          location="Eagle Ridge",
          travel_min=38, travel_km=27.8),
        E(6, 11, 0,  2.0, "Sunday walk",            category="family"),
    ],
]


def events_for_week(week_index: int) -> list[dict]:
    """Merge the always-on routines with the variant for this week.
    week_index is 0-based; cycles through WEEK_VARIANTS so seeding
    more weeks than variants just repeats the pattern."""
    variant = WEEK_VARIANTS[week_index % len(WEEK_VARIANTS)]
    return WEEKLY_ROUTINES + variant


def category_color(cat: str) -> str:
    """Pick a quiet tint per category so the calendar grid reads in
    colour even on installs where the per-category renderer isn't on.
    Matches event_categories.py palette intent."""
    return {
        "family":   "#34d399",  # emerald
        "business": "#64748b",  # slate
        "drive":    "#f59e0b",  # amber
        "health":   "#fb7185",  # rose
        "personal": "#a78bfa",  # violet (matches existing seed default)
        "social":   "#38bdf8",  # sky
    }.get(cat, "#a78bfa")


def events_table_has_category(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info(events)").fetchall()
    return any(r["name"] == "category" for r in rows)


def events_table_has_travel(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info(events)").fetchall()
    return any(r["name"] == "travel_time_s" for r in rows)


def insert_events(conn: sqlite3.Connection, owner_id: int,
                   week_monday: date, week_index: int) -> int:
    row = conn.execute(
        "SELECT id FROM calendars "
        "WHERE owner_user_id = ? AND kind = 'personal' LIMIT 1",
        (owner_id,),
    ).fetchone()
    cal_id = row["id"] if row else None

    has_category = events_table_has_category(conn)
    has_travel   = events_table_has_travel(conn)
    computed_at  = datetime.now().isoformat(timespec="seconds")

    added = 0
    for e in events_for_week(week_index):
        day = week_monday + timedelta(days=e["weekday"])
        starts_dt = datetime(day.year, day.month, day.day, e["hh"], e["mm"])
        ends_dt   = starts_dt + timedelta(hours=e["duration_h"])
        starts_iso = starts_dt.isoformat(timespec="seconds")
        ends_iso   = ends_dt.isoformat(timespec="seconds")

        # Idempotency on (title, starts_at) — re-running on the same
        # anchor week is a no-op; running with a different anchor
        # creates a fresh set on the new dates without disturbing
        # whatever's already there.
        existing = conn.execute(
            "SELECT id FROM events "
            "WHERE title = ? AND starts_at = ? AND notes LIKE ?",
            (e["title"], starts_iso, f"%{MARKER}%"),
        ).fetchone()
        if existing:
            continue

        color = category_color(e["category"])
        # Migration 037 dropped allowed_roles in favour of calendar-
        # level ACL via calendar_id; insert without that column.
        if has_category:
            conn.execute(
                "INSERT INTO events "
                "  (title, starts_at, ends_at, all_day, color, person, "
                "   notes, calendar_id, owner_user_id, "
                "   visibility, location, category) "
                "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, "
                "        'default', ?, ?)",
                (e["title"], starts_iso, ends_iso, color,
                 e.get("person"), e["notes"],
                 cal_id, owner_id, e.get("location"), e["category"]),
            )
        else:
            conn.execute(
                "INSERT INTO events "
                "  (title, starts_at, ends_at, all_day, color, person, "
                "   notes, calendar_id, owner_user_id, "
                "   visibility, location) "
                "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, "
                "        'default', ?)",
                (e["title"], starts_iso, ends_iso, color,
                 e.get("person"), e["notes"],
                 cal_id, owner_id, e.get("location")),
            )
        # Backfill travel data on the just-inserted row so the calendar
        # badge ("18 min · 9.2 km · via ORS") renders without needing
        # the maps connector configured. Skipped silently on schemas
        # pre-migration 019 (no travel_* columns).
        main_event_id = None
        if has_travel and e.get("travel_min") and e.get("travel_km") and e.get("location"):
            main_event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "UPDATE events SET "
                "  travel_time_s = ?, travel_distance_m = ?, "
                "  travel_provider = 'ors', travel_computed_at = ? "
                "WHERE id = ?",
                (int(e["travel_min"]) * 60,
                 int(float(e["travel_km"]) * 1000),
                 computed_at,
                 main_event_id),
            )
        added += 1

        # Drive-time blocks: insert separate "Drive to: <title>" /
        # "Drive back: <title>" events in the same shape
        # backend/skills/block_travel_time/skill.py produces. This is
        # what's actually visible in the calendar grid — the
        # travel_time_s column above only drives the badge in the
        # day-list view + event dialog. Notes carry the LINKED_TO
        # marker (so a future delete cascade works) AND our YORIK_
        # DEMO_WEEK marker (so the cleanup DELETE catches these too).
        # Return blocks add `[DIR=return]` so forward + return can
        # coexist without the dedupe in the skill collapsing them.
        #
        # Threshold: skip blocks under 16 min. Short trips read as
        # cosmetic clutter in the grid (a 6-minute "Drive to: Farmers'
        # market" chip stacked under the actual market chip doesn't
        # earn its visual weight). Matches the >15 min rule the user
        # wants in production.
        if main_event_id is not None and int(e["travel_min"]) > 15:
            minutes = int(e["travel_min"])
            link_marker = f"[LINKED_TO={main_event_id}]"
            for direction in ("forward", "return"):
                if direction == "return":
                    block_start = ends_dt
                    block_end   = ends_dt + timedelta(minutes=minutes)
                    block_title = f"Drive back: {e['title']}"
                    block_notes = f"{link_marker} [DIR=return] {MARKER}"
                else:
                    block_start = starts_dt - timedelta(minutes=minutes)
                    block_end   = starts_dt
                    block_title = f"Drive to: {e['title']}"
                    block_notes = f"{link_marker} {MARKER}"

                if has_category:
                    conn.execute(
                        "INSERT INTO events "
                        "  (title, starts_at, ends_at, all_day, person, "
                        "   notes, calendar_id, owner_user_id, "
                        "   visibility, category) "
                        "VALUES (?, ?, ?, 0, NULL, ?, ?, ?, 'default', 'drive')",
                        (block_title,
                         block_start.isoformat(timespec="seconds"),
                         block_end.isoformat(timespec="seconds"),
                         block_notes,
                         cal_id, owner_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO events "
                        "  (title, starts_at, ends_at, all_day, person, "
                        "   notes, calendar_id, owner_user_id, "
                        "   visibility) "
                        "VALUES (?, ?, ?, 0, NULL, ?, ?, ?, 'default')",
                        (block_title,
                         block_start.isoformat(timespec="seconds"),
                         block_end.isoformat(timespec="seconds"),
                         block_notes,
                         cal_id, owner_id),
                    )
                added += 1
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner", type=int, default=1,
        help="user_id that owns the seeded rows (default: 1)",
    )
    parser.add_argument(
        "--anchor", type=str, default=None,
        help="Any date in the FIRST week you want filled (YYYY-MM-DD). "
             "Defaults to this week's Monday.",
    )
    parser.add_argument(
        "--weeks", type=int, default=1,
        help="Number of consecutive weeks to seed starting from anchor "
             "(default: 1). Use 2 to fill this week AND next week.",
    )
    args = parser.parse_args()

    if args.weeks < 1:
        print("--weeks must be >= 1", file=sys.stderr)
        return 1

    if args.anchor:
        try:
            anchor = date.fromisoformat(args.anchor)
        except ValueError:
            print(f"--anchor must be YYYY-MM-DD, got {args.anchor!r}",
                  file=sys.stderr)
            return 1
        first_monday = monday_of_week(anchor)
    else:
        first_monday = this_week_monday(date.today())

    db = resolve_db_path()
    if not db.exists():
        print(f"DB not found at {db}", file=sys.stderr)
        print("Run the app once first so the schema is created.",
              file=sys.stderr)
        return 1

    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row

    if not conn.execute(
        "SELECT 1 FROM user_profiles WHERE id = ?", (args.owner,),
    ).fetchone():
        print(
            f"No user_profiles row with id={args.owner}. "
            "Register a user via the web UI first, "
            "or pass --owner <id>.",
            file=sys.stderr,
        )
        return 1

    print(f"DB:        {db}")
    print(f"Owner:     user_id={args.owner}")
    print(f"Weeks:     {args.weeks}")

    before = conn.execute(
        "SELECT COUNT(*) FROM events WHERE notes LIKE ?",
        (f"%{MARKER}%",),
    ).fetchone()[0]

    total_added = 0
    for i in range(args.weeks):
        week_monday = first_monday + timedelta(days=7 * i)
        sunday = week_monday + timedelta(days=6)
        added = insert_events(conn, args.owner, week_monday, i)
        total_added += added
        variant_label = chr(ord('A') + (i % len(WEEK_VARIANTS)))
        print(f"  week {i+1} ({variant_label}): "
              f"{week_monday.isoformat()} → {sunday.isoformat()}  +{added}")
    conn.commit()

    print()
    print(f"events:    +{total_added}  (had {before} of these already)")
    print()
    print("Removal SQL:")
    print(f"  DELETE FROM events WHERE notes LIKE '%{MARKER}%';")
    return 0


if __name__ == "__main__":
    sys.exit(main())
