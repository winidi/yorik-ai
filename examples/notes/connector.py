"""Notes — reference app showing the Yorik App SDK in action.

Three @operation functions the main Yorik agent can call via
trigger_connector("notes.<op_name>", ...):
  - add_note(body)           — saves a note, infers mood via LLM
  - list_notes(limit=20)     — most recent notes
  - summarize_today()        — one-paragraph summary via LLM

Each operation runs against this app's private SQLite at
data/apps/notes/data.db. No access to family.db.
"""

from yorik.app_sdk import operation, db, llm


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

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO notes (body, mood) VALUES (?, ?)",
            (body, mood),
        )
        conn.commit()
        return {"id": cur.lastrowid, "body": body, "mood": mood}


@operation(role=["admin", "member"], description="List the most recent notes. Returns up to 100.")
def list_notes(limit: int = 20) -> dict:
    n = max(1, min(int(limit or 20), 100))
    with db() as conn:
        rows = conn.execute(
            "SELECT id, body, mood, created_at FROM notes "
            "ORDER BY created_at DESC LIMIT ?",
            (n,),
        ).fetchall()
    return {"notes": [dict(r) for r in rows], "count": len(rows)}


@operation(role=["admin", "member"], description="Summarize today's notes in one short paragraph via the LLM.")
def summarize_today() -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT body FROM notes "
            "WHERE date(created_at) = date('now') "
            "ORDER BY created_at ASC"
        ).fetchall()
    if not rows:
        return {"summary": "No notes today.", "note_count": 0}
    bodies = "\n".join(f"- {r['body']}" for r in rows)
    summary = llm.ask(
        f"Summarize today's notes:\n\n{bodies}",
        system="One short paragraph. Two or three sentences. No bullet list, no preamble.",
    ).strip()
    return {"summary": summary, "note_count": len(rows)}
