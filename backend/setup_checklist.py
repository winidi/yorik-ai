"""The Home checklist ("Set up Yorik") and the per-person UI memory
behind the first-run tour and the one-time app hints.

GET   /api/setup/checklist   steps for the signed-in person, each done or not
GET   /api/me/ui-state       {tour_done, checklist_hidden, hints: [...]}
PATCH /api/me/ui-state       merge in any of those keys

Steps are read from real data, never ticked by hand, so a step done
elsewhere (mail added from the Email app) shows as done here too.
Which steps a person sees depends on the role: children get the short
list, the admin also gets family, calendar and backups.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .auth_sessions import current_user
from .database import get_conn

log = logging.getLogger("yorik.setup")

router = APIRouter(prefix="/api", tags=["setup"])


def _count(conn, sql: str, params: tuple) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
        return int(list(row.values())[0] if isinstance(row, dict) else row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001 — a missing table must not break Home
        log.info("checklist probe failed (%s): %s", sql[:60], exc)
        return 0


def _state(user_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT ui_state FROM user_profiles WHERE id = ?", (user_id,)).fetchone()
    try:
        return json.loads((row or {}).get("ui_state") or "{}") if row else {}
    except ValueError:
        return {}


@router.get("/setup/checklist")
def checklist(user: dict = Depends(current_user)) -> dict[str, Any]:
    uid = user["id"]
    role = (user.get("role") or "").lower()
    admin = role in ("admin", "platform_admin")
    kid = role in ("restricted", "child")
    with get_conn() as conn:
        prof = conn.execute("SELECT color, avatar_at FROM user_profiles WHERE id = ?", (uid,)).fetchone() or {}
        asked = _count(conn, "SELECT COUNT(*) FROM agent_conversations WHERE user_id = ?", (uid,))
        phone = _count(conn, "SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?", (uid,))
        mail = _count(conn, "SELECT COUNT(*) FROM email_accounts WHERE owner_user_id = ?", (uid,))
        cal = _count(conn, "SELECT COUNT(*) FROM calendar_feeds WHERE owner_user_id = ?", (uid,))
        people = _count(conn, "SELECT COUNT(*) FROM user_profiles WHERE COALESCE(disabled, 0) = 0", ())

    steps: list[dict[str, Any]] = [
        {"id": "look", "title": "Your colour and photo", "hint": "So the family recognises you",
         "done": bool(prof.get("color") or prof.get("avatar_at")), "action": "profile"},
        {"id": "ask", "title": "Ask Yorik something", "hint": "Try: remind me tomorrow at 9",
         "done": asked > 0, "action": "chat"},
        {"id": "phone", "title": "Yorik on your phone", "hint": "Home screen and reminders",
         "done": phone > 0, "action": "phone"},
    ]
    if admin:
        steps.append({"id": "family", "title": "Invite your family", "hint": "A QR code per person, two minutes",
                      "done": people > 1, "action": "invite"})
    if not kid:
        steps.append({"id": "mail", "title": "Connect your email", "hint": "Gmail, GMX, Web.de, iCloud …",
                      "done": mail > 0, "action": "mail"})
        steps.append({"id": "calendar", "title": "Bring in your calendar", "hint": "Your Google or iPhone dates",
                      "done": cal > 0, "action": "calendar"})
    if admin:
        try:
            from . import backup
            cfg = backup.get_config()
            backed = bool(cfg.get("passphrase_set") and cfg.get("schedule"))
        except Exception:  # noqa: BLE001
            backed = False
        steps.append({"id": "backup", "title": "Turn on backups", "hint": "So nothing gets lost",
                      "done": backed, "action": "backup"})
    st = _state(uid)
    return {"steps": steps, "done": sum(1 for s in steps if s["done"]), "total": len(steps),
            "hidden": bool(st.get("checklist_hidden"))}


@router.get("/me/ui-state")
def get_ui_state(user: dict = Depends(current_user)) -> dict[str, Any]:
    st = _state(user["id"])
    return {"tour_done": bool(st.get("tour_done")), "checklist_hidden": bool(st.get("checklist_hidden")),
            "hints": list(st.get("hints") or [])}


class UiStatePatch(BaseModel):
    tour_done: Optional[bool] = None
    checklist_hidden: Optional[bool] = None
    hint_seen: Optional[str] = None


@router.patch("/me/ui-state")
def patch_ui_state(body: UiStatePatch, user: dict = Depends(current_user)) -> dict[str, Any]:
    st = _state(user["id"])
    if body.tour_done is not None:
        st["tour_done"] = body.tour_done
    if body.checklist_hidden is not None:
        st["checklist_hidden"] = body.checklist_hidden
    if body.hint_seen:
        hints = set(st.get("hints") or [])
        hints.add(body.hint_seen[:40])
        st["hints"] = sorted(hints)
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET ui_state = ? WHERE id = ?", (json.dumps(st), user["id"]))
        conn.commit()
    return get_ui_state(user)
