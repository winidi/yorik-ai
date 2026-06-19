#!/usr/bin/env python3
"""Seed a fresh Yorik install with demo data so the agent has something to
chain off of out of the box.

Inserts ~22 contacts, ~23 calendar events (mix of past / today / next month /
far future), and ~12 tasks. Every row carries the marker

    [YORIK_DEMO v1]

in its `notes` column, so removing the whole set later is a single
DELETE per table (see bottom of file for the exact commands).

Usage
-----
    # from the project root, with the venv active:
    python scripts/seed-demo-data.py             # owner = user_id 1
    python scripts/seed-demo-data.py --owner 2   # if user 1 is reserved

Notes
-----
* All names are deliberately fictional (placeholders + "Mustermann"-style
  fakes). No real people, no real businesses.
* Idempotent: re-running the script is a no-op once the rows exist.
* The script does NOT manage the uvicorn process. SQLite is shared so
  the running backend picks up the new rows immediately.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

MARKER = "[YORIK_DEMO v1]"

# Resolve the DB path the same way backend.database does: prefer the
# env override, otherwise relative to the project root (script's parent).
def resolve_db_path() -> Path:
    raw = os.environ.get("HOMEOS_DB_PATH", "data/family.db")
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


now = datetime.now()
today = now.date()


def days(n: int) -> str:
    return (today + timedelta(days=n)).isoformat()


def hour(date_iso: str, h: int, mm: int = 0) -> str:
    return f"{date_iso}T{h:02d}:{mm:02d}:00"


# ── Schema cheatsheet (sanity, not enforced here) ────────────────────
# contacts          : display_name, kind, status, relation, notes, source,
#                     allowed_roles, created_by_user_id
# contact_channels  : contact_id, kind (email/phone/whatsapp/…), value, source
# contact_addresses : contact_id, kind, line1, postcode, city, country, source
# events            : title, starts_at, ends_at, all_day, color, person, notes,
#                     calendar_id, owner_user_id, visibility, location
# tasks             : title, due_date, person, allowed_roles, done, notes

# 22 contacts spanning family / friends / providers / businesses.
# Surnames are stock fakes ("Mustermann", "Beispielmann", "Probetestin").
# Multiple "Hans …" entries on purpose, so the disambiguation UI gets
# exercised right out of the gate.
CONTACTS = [
    # ── Family-ish placeholders ──────────────────────────────────────
    ("Mom",                           "person",   "Mother",
        {"phone": "+1 555 0101234"},  "7 Example Ave, Springfield"),
    ("Dad",                           "person",   "Father",
        {"phone": "+1 555 0102345"},  "7 Example Ave, Springfield"),
    ("Lena",                          "person",   "Sister",
        {"whatsapp": "15550110123@s.whatsapp.net"},  None),
    ("Tobias",                        "person",   "Brother",
        {"email": "tobias@example.test"},  None),
    # ── Friends with disambiguation pressure ────────────────────────
    ("John Miller",                   "person",   "School friend",
        {"phone": "+1 555 0198765"},  "12 Lime St, Springfield"),
    ("John Baker",                    "person",   "Neighbor",
        {"phone": "+1 555 0112345"},  "4 Oak Way, Springfield"),
    ("John Smith",                    "person",   "Coworker",
        {"email": "john.smith@example.test"},
        "5 Office Plaza, Riverdale"),
    ("Maja Tester",                   "person",   "Friend",
        {"email": "maja@example.test"},  "5 Sample St, Springfield"),
    ("Lisa Demo",                     "person",   "Coworker",
        {"email": "lisa.demo@example.test"},  "14 Example Way, Riverdale"),
    ("Marco Example",                 "person",   "Friend in Italy",
        {"email": "marco.example@example.it"},  "Via Esempio 3, 00184 Roma"),
    ("Felix Sample",                  "person",   "Training partner",
        {"phone": "+1 555 0100123"},  None),
    # ── Health-care providers ───────────────────────────────────────
    ("Dr. Sabine Example",            "person",   "Dentist",
        {"phone": "+1 555 0155010"}, "18 Station Rd, Springfield"),
    ("Dr. Mark Example",              "person",   "GP",
        {"phone": "+1 555 0133020"}, "22 Main St, Springfield"),
    ("Example Veterinary",            "business", "Veterinarian",
        {"phone": "+1 555 0199030"}, "9 Example Way, Springfield"),
    # ── Local services ──────────────────────────────────────────────
    ("Example Hair Studio",           "business", "Barber",
        {"phone": "+1 555 0122334"}, "7 Market St, Springfield"),
    ("Example Auto Service",          "business", "Garage",
        {"phone": "+1 555 0198877"}, "14 Industry Ring, Springfield"),
    ("Example Pharmacy",              "business", "Pharmacy",
        {"phone": "+1 555 0178899"}, "2 Town Square, Springfield"),
    # ── Formal correspondents ───────────────────────────────────────
    ("Example Property Mgmt",         "business", "Landlord",
        {"email": "contact@property-mgmt.example.test"},
        "10 Admin St, Springfield"),
    ("Springfield Utilities",         "business", "Utility provider",
        {"email": "service@utilities.example.test"},
        "1 Works Rd, Springfield"),
    ("Example Health Insurance",      "business", "Health insurer",
        {"phone": "+1 800 1111000"}, None),
    ("Example Bank Springfield",      "business", "Bank",
        {"phone": "+1 555 0105050"}, "5 Station Rd, Springfield"),
    ("Example Tax Advisors",          "business", "Accountant",
        {"email": "office@example.test"},
        "8 Counsel St, Springfield"),
]


def E(title, days_offset, hour_h, hour_m=0, duration_h=1,
      person=None, location=None, notes=""):
    starts_at = hour(days(days_offset), hour_h, hour_m)
    ends_dt = (datetime.fromisoformat(starts_at)
               + timedelta(hours=duration_h))
    return {
        "title":     title,
        "starts_at": starts_at,
        "ends_at":   ends_dt.isoformat(timespec="seconds"),
        "person":    person,
        "location":  location,
        "notes":     (notes + " " + MARKER).strip(),
    }


# 23 events, weighted toward "this week / next month" so the calendar
# UI looks alive on first launch.
EVENTS = [
    # ── Past (last ~30 days) ────────────────────────────────────────
    E("Dentist checkup",                -28, 14, person="Dr. Example",
      location="18 Station Rd, Springfield"),
    E("Haircut + beard trim",           -14, 10,
      location="Example Hair Studio, Springfield"),
    E("Run club",                        -7, 18, person="Felix",
      location="City Park, Springfield"),
    E("Q1 tax docs handover",           -10,  9,
      location="Example Tax Advisors"),
    E("Concert with Aunt Clara",        -21, 19, person="Lena",
      location="Culture House, Springfield"),
    # ── This week ───────────────────────────────────────────────────
    E("Team standup",                    -1,  9, person="all"),
    E("Lunch with John Miller",           0, 12, person="John Miller",
      location="Café Example, Springfield"),
    E("Yoga",                             1, 18, location="Community Center"),
    E("Visit parents",                    2, 14, duration_h=3,
      person="Mom, Dad",
      location="7 Example Ave, Springfield"),
    E("Farmers' market",                  3, 10,
      location="Town Square, Springfield"),
    # ── Next week ───────────────────────────────────────────────────
    E("Vet — vaccinations for Bello",     5, 11, person="Vet",
      location="9 Example Way, Springfield"),
    E("Garage — annual inspection",       6,  8,
      location="Example Auto Service, Springfield"),
    E("Dinner with Lisa Demo",            7, 19, duration_h=2,
      person="Lisa Demo",
      location="Restaurant Example, Springfield"),
    E("GP — annual checkup",              9, 10, person="Dr. Example",
      location="22 Main St, Springfield"),
    E("School disco",                    10, 16, duration_h=2, person="child",
      location="Springfield Elementary"),
    # ── Two to four weeks out ──────────────────────────────────────
    E("Health insurance meeting",        14, 13, person="Example Health Insurance"),
    E("Tobias' birthday",                18, 18, duration_h=4, person="Tobias"),
    E("Annual landlord review",          21, 15,
      location="10 Admin St, Springfield"),
    E("Dentist — checkup",               24, 14, person="Dr. Example",
      location="18 Station Rd, Springfield"),
    E("Q3 strategy workshop",            28,  9, duration_h=8, person="all",
      location="Office"),
    # ── Far future ─────────────────────────────────────────────────
    E("Summer vacation — Italy",         60,  8, duration_h=240, person="Marco",
      location="Roma, Italy"),
    E("Q3 tax docs submission",          90, 11),
    E("Tobias' wedding",                120, 14, person="Tobias",
      location="Example Castle"),
]


# 12 tasks: a couple of done items, a span of open ones across the
# next three weeks, plus two overdue rows so the dashboard's
# "overdue" branch has data.
TASKS = [
    ("Take out the trash",                        -2, "all",     True),
    ("Weekend groceries",                         -3, "all",     True),
    ("Finish Q1 tax return",                       3, "admin",   False),
    ("Buy a gift for Tobias",                     14, "admin",   False),
    ("Write rent-reduction letter to landlord",    5, "admin",   False),
    ("Take the car in for inspection",             5, "all",     False),
    ("Service the dishwasher",                     7, "all",     False),
    ("Trim the garden hedge",                      4, "member",  False),
    ("Buy school backpack",                       21, "child",   False),
    ("Reply to Tobias' wedding invitation",       10, "admin",   False),
    ("Pay the garage invoice",                    -3, "admin",   False),
    ("Pick up the insurance certificate",         -7, "admin",   False),
]


def insert_contacts(c: sqlite3.Connection, owner_id: int) -> int:
    added = 0
    for display_name, kind, relation, channels, address in CONTACTS:
        existing = c.execute(
            "SELECT id FROM contacts "
            "WHERE display_name = ? AND notes LIKE ?",
            (display_name, f"%{MARKER}%"),
        ).fetchone()
        if existing:
            continue
        cur = c.execute(
            "INSERT INTO contacts "
            "  (display_name, kind, status, relation, allowed_roles, "
            "   notes, source, created_by_user_id) "
            "VALUES (?, ?, 'active', ?, 'admin,member', ?, 'manual', ?)",
            (display_name, kind, relation, MARKER, owner_id),
        )
        cid = cur.lastrowid
        added += 1
        for ch_kind, value in (channels or {}).items():
            c.execute(
                "INSERT INTO contact_channels "
                "  (contact_id, kind, value, source) "
                "VALUES (?, ?, ?, 'manual')",
                (cid, ch_kind, value),
            )
        if address:
            parts = [p.strip() for p in address.split(",")]
            line1 = parts[0] if parts else ""
            pc_city = parts[1] if len(parts) > 1 else ""
            country = parts[2] if len(parts) > 2 else "DE"
            pcode, city = "", pc_city
            tokens = pc_city.split(" ", 1)
            if tokens and tokens[0].isdigit():
                pcode = tokens[0]
                city = tokens[1] if len(tokens) > 1 else ""
            try:
                c.execute(
                    "INSERT INTO contact_addresses "
                    "  (contact_id, kind, line1, postcode, city, country, "
                    "   source) "
                    "VALUES (?, 'home', ?, ?, ?, ?, 'manual')",
                    (cid, line1, pcode, city, country),
                )
            except sqlite3.OperationalError:
                pass  # schema variant — silently skip the address
    return added


def insert_events(c: sqlite3.Connection, owner_id: int) -> int:
    row = c.execute(
        "SELECT id FROM calendars "
        "WHERE owner_user_id = ? AND kind = 'personal' LIMIT 1",
        (owner_id,),
    ).fetchone()
    cal_id = row["id"] if row else None
    added = 0
    for e in EVENTS:
        existing = c.execute(
            "SELECT id FROM events "
            "WHERE title = ? AND notes LIKE ?",
            (e["title"], f"%{MARKER}%"),
        ).fetchone()
        if existing:
            continue
        c.execute(
            "INSERT INTO events "
            "  (title, starts_at, ends_at, all_day, color, person, "
            "   notes, allowed_roles, calendar_id, owner_user_id, "
            "   visibility, location) "
            "VALUES (?, ?, ?, 0, '#a78bfa', ?, ?, 'admin,member', ?, ?, "
            "        'default', ?)",
            (e["title"], e["starts_at"], e["ends_at"],
             e.get("person"), e["notes"],
             cal_id, owner_id, e.get("location")),
        )
        added += 1
    return added


def insert_tasks(c: sqlite3.Connection) -> int:
    added = 0
    for title, days_offset, person, done in TASKS:
        existing = c.execute(
            "SELECT id FROM tasks "
            "WHERE title = ? AND notes LIKE ?",
            (title, f"%{MARKER}%"),
        ).fetchone()
        if existing:
            continue
        c.execute(
            "INSERT INTO tasks "
            "  (title, due_date, person, allowed_roles, done, notes) "
            "VALUES (?, ?, ?, 'admin,member', ?, ?)",
            (title, days(days_offset), person, 1 if done else 0, MARKER),
        )
        added += 1
    return added


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner", type=int, default=1,
        help="user_id that owns the seeded rows (default: 1)",
    )
    args = parser.parse_args()

    db = resolve_db_path()
    if not db.exists():
        print(f"DB not found at {db}", file=sys.stderr)
        print("Run the app once first so the schema is created.",
              file=sys.stderr)
        return 1

    c = sqlite3.connect(db, timeout=10)
    c.row_factory = sqlite3.Row

    if not c.execute(
        "SELECT 1 FROM user_profiles WHERE id = ?", (args.owner,),
    ).fetchone():
        print(
            f"No user_profiles row with id={args.owner}. "
            "Register a user via the web UI first, "
            "or pass --owner <id>.",
            file=sys.stderr,
        )
        return 1

    print(f"DB:    {db}")
    print(f"Owner: user_id={args.owner}\n")

    print("BEFORE:")
    for t in ("contacts", "events", "tasks"):
        n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<10} {n}")

    n_c = insert_contacts(c, args.owner)
    n_e = insert_events(c, args.owner)
    n_t = insert_tasks(c)
    c.commit()

    print("\nINSERTED:")
    print(f"  contacts   {n_c}")
    print(f"  events     {n_e}")
    print(f"  tasks      {n_t}")

    print("\nAFTER:")
    for t in ("contacts", "events", "tasks"):
        n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<10} {n}")

    print(f"\nDemo rows are tagged with {MARKER!r}. Remove them later with:")
    print(f"  DELETE FROM contacts WHERE notes LIKE '%{MARKER}%';")
    print(f"  DELETE FROM events   WHERE notes LIKE '%{MARKER}%';")
    print(f"  DELETE FROM tasks    WHERE notes LIKE '%{MARKER}%';")
    return 0


if __name__ == "__main__":
    sys.exit(main())
