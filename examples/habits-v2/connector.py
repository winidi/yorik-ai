"""Habits — minimum-viable manifest v2 reference app.

Three @operation functions:
  - add_habit(name, target_per_week=1)
  - log_completion(habit_id, note=None)
  - list_habits()

Each runs against the app's own Postgres schema via pg_db().
"""

from yorik.app_sdk import operation, pg_db


@operation(role=["admin", "member"], description="Add a new habit to track.")
def add_habit(name: str, target_per_week: int = 1) -> dict:
    name = (name or "").strip()
    if not name:
        return {"error": "name required"}
    target = max(1, min(int(target_per_week or 1), 21))
    with pg_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO habits (name, target_per_week) "
                "VALUES (%s, %s) RETURNING id, name, target_per_week",
                (name, target),
            )
            row = cur.fetchone()
            return {"id": row[0], "name": row[1], "target_per_week": row[2]}


@operation(role=["admin", "member"], description="Log a completion of an existing habit.")
def log_completion(habit_id: int, note: str = None) -> dict:
    with pg_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO habit_completions (habit_id, note) "
                "VALUES (%s, %s) RETURNING id, habit_id, completed_at",
                (int(habit_id), note),
            )
            row = cur.fetchone()
            if row is None:
                return {"error": f"habit {habit_id} not found"}
            return {
                "id": row[0], "habit_id": row[1],
                "completed_at": row[2].isoformat(),
            }


@operation(role=["admin", "member"], description="List habits with completion count from the past 7 days.")
def list_habits() -> dict:
    with pg_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT h.id, h.name, h.target_per_week, "
                "       COUNT(c.id) FILTER (WHERE c.completed_at > now() - interval '7 days') "
                "  FROM habits h "
                "  LEFT JOIN habit_completions c ON c.habit_id = h.id "
                "  GROUP BY h.id, h.name, h.target_per_week "
                "  ORDER BY h.id"
            )
            rows = cur.fetchall()
    return {
        "habits": [
            {
                "id": r[0], "name": r[1],
                "target_per_week": r[2],
                "completions_last_7d": int(r[3] or 0),
            }
            for r in rows
        ],
    }
