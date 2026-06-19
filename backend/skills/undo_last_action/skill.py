"""undo_last_action — voice-/chat-triggered rollback of the most recent mutation.

Wraps the same machinery as the Cancel button on the confirm-mutation
modal: looks up the user's most recent pending_actions row, dispatches
its `rollback_kind` via pending_actions.rollback(), drops the row, and
records telemetry as 'cancelled'. Cap at 60 minutes so a stray "undo"
hours later doesn't surprise the user.
"""
from __future__ import annotations
from typing import Any

UNDO_WINDOW_SECONDS = 60 * 60  # only undo actions younger than 60 min


async def execute(ctx) -> dict[str, Any]:
    user_id = getattr(ctx, "user_id", None)
    if user_id is None:
        raise ValueError("undo_last_action requires ctx.user_id")

    from backend.database import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, skill, preview_json, rollback_kind, created_at, "
            "       llm_model, language, params_json, "
            "       CAST((julianday('now') - julianday(created_at)) * 86400 AS INTEGER) AS age_s "
            "FROM pending_actions "
            "WHERE user_id = ? "
            "  AND rollback_kind != '' "
            "  AND rollback_kind != '(none)' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    if not row:
        return {
            "undone":      "",
            "skill":       "",
            "preview":     {},
            "age_seconds": 0,
            "message":     "no recent action to undo",
        }

    age_s = int(row["age_s"] or 0)
    if age_s > UNDO_WINDOW_SECONDS:
        return {
            "undone":      "",
            "skill":       row["skill"],
            "preview":     {},
            "age_seconds": age_s,
            "message":     (
                f"the most recent action ({row['skill']}) is "
                f"{age_s // 60} minutes old — too far back to undo "
                "automatically. Ask the user to confirm the rollback "
                "manually if they really want it reverted."
            ),
        }

    import json as _json
    preview = {}
    try:
        preview = _json.loads(row["preview_json"] or "{}")
    except _json.JSONDecodeError:
        pass

    from backend import pending_actions as pa
    rollback_result = pa.rollback(row["id"])
    pa.drop(row["id"])
    pa.record_decision(
        skill=row["skill"], decision="cancelled",
        user_id=user_id, llm_model=row["llm_model"] or "",
        language=row["language"] or "en",
        params=_json.loads(row["params_json"] or "{}"),
    )

    return {
        "undone":      rollback_result.get("undone") or row["rollback_kind"],
        "skill":       row["skill"],
        "preview":     preview,
        "age_seconds": age_s,
        "message":     f"reverted {row['skill']} (was {age_s}s old)",
    }
