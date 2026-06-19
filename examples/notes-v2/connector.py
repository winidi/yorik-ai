"""Notes (v2) — reference app for the Phase E platform SDK.

Uses pg_db() to talk to the app's own Postgres schema. The owned
schema is `app_yorik_notes` (declared in manifest.json); pg_db sets
search_path so `INSERT INTO notes (...)` lands in that schema.

Three operations the agent or the iframe can call:
  - add_note(body)        — saves a note; infers mood via LLM
  - list_notes(limit=20)  — most recent notes
  - summarize_today()     — one-paragraph summary via LLM
"""

from yorik.app_sdk import operation, pg_db, llm


_VALID_MOODS = {"happy", "sad", "anxious", "neutral", "excited",
                "calm", "frustrated", "grateful", "tired"}


@operation(role=["admin", "member"], description="Save a quick note. Optionally infers a one-word mood label via the LLM.")
def add_note(body: str) -> dict:
    body = (body or "").strip()
    if not body:
        return {"error": "body required"}

    mood = None
    if len(body) >= 20:
        try:
            raw = llm.ask(
                f"What single-word mood does this note express?\n\nNote: {body}",
                system=(
                    "Reply with ONE word from this list, lowercase, no punctuation: "
                    + ", ".join(sorted(_VALID_MOODS))
                ),
            ).strip().lower().split()[0]
            raw = "".join(c for c in raw if c.isalpha())[:20]
            mood = raw if raw in _VALID_MOODS else None
        except Exception:
            mood = None

    with pg_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notes (body, mood) VALUES (%s, %s) "
                "RETURNING id, body, mood",
                (body, mood),
            )
            row = cur.fetchone()
            return {"id": row[0], "body": row[1], "mood": row[2]}


@operation(role=["admin", "member"], description="List the most recent notes. Returns up to 100.")
def list_notes(limit: int = 20) -> dict:
    n = max(1, min(int(limit or 20), 100))
    with pg_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, body, mood, created_at FROM notes "
                "ORDER BY created_at DESC LIMIT %s",
                (n,),
            )
            rows = cur.fetchall()
    notes = [
        {"id": r[0], "body": r[1], "mood": r[2], "created_at": r[3].isoformat()}
        for r in rows
    ]
    return {"notes": notes, "count": len(notes)}


@operation(role=["admin", "member"], description="Summarize today's notes in one short paragraph via the LLM.")
def summarize_today() -> dict:
    with pg_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT body FROM notes "
                "WHERE created_at::date = current_date "
                "ORDER BY created_at ASC"
            )
            rows = cur.fetchall()
    if not rows:
        return {"summary": "No notes today.", "note_count": 0}
    bodies = "\n".join(f"- {r[0]}" for r in rows)
    summary = llm.ask(
        f"Summarize today's notes:\n\n{bodies}",
        system="One short paragraph. Two or three sentences. No bullet list, no preamble.",
    ).strip()
    return {"summary": summary, "note_count": len(rows)}
