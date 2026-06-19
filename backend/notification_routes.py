"""Notification REST API — feeds the bell icon in the React shell."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth_sessions import current_user
from .database import get_conn
from . import notifications as notif
from . import email_blocklist

log = logging.getLogger("yorik.notification_routes")

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def list_notifications(
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(current_user),
):
    return {
        "notifications": notif.list_for_user(user["id"], unread_only=unread_only, limit=limit),
        "unread_count": notif.unread_count(user["id"]),
    }


@router.post("/{notification_id}/read")
def mark_one_read(notification_id: int, user: dict = Depends(current_user)):
    notif.mark_read(user["id"], [notification_id])
    return {"ok": True}


@router.post("/mark-all-read")
def mark_all(user: dict = Depends(current_user)):
    n = notif.mark_all_read(user["id"])
    return {"ok": True, "marked": n}


@router.post("/{notification_id}/accept")
async def accept_proposal(notification_id: int, user: dict = Depends(current_user)):
    """Accept an actionable notification (kind='email_proposal'): dispatches
    to the corresponding skill with the extracted payload. The skill's
    apply-then-rollback pattern means the row is inserted immediately and
    the user can still cancel via the apply-rollback flow if extraction
    was wrong."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, kind, payload_json FROM notifications "
            "WHERE id = ? AND user_id = ?",
            (notification_id, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "notification not found")
    if row["kind"] != "email_proposal":
        raise HTTPException(400, f"notification kind '{row['kind']}' is not acceptable")
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        raise HTTPException(500, "notification payload corrupted")

    category = payload.get("category")
    extracted = payload.get("extracted") or {}
    result: dict
    if category == "bill":
        # Map payload → add_bill skill inputs. If extraction missed the
        # due date, default to today+30d — the apply-rollback panel lets
        # the user fix it (or cancel) before it sticks.
        from datetime import date as _date, timedelta as _td
        due = extracted.get("due_date") or (_date.today() + _td(days=30)).isoformat()
        skill_args = {
            "name":             payload.get("subject") or f"Bill from {payload.get('vendor', 'unknown')}",
            "amount":           extracted.get("amount") or 0,
            "currency":         extracted.get("currency", "EUR"),
            "due_date":         due,
            "notes":            f"Auto-imported from email (notif #{notification_id})",
            "email_message_id": payload.get("message_id"),
            "document_id":      payload.get("document_id"),
        }
        result = await _run_skill("add_bill", skill_args, user)
    elif category == "appointment":
        date = extracted.get("date")
        time = extracted.get("time") or "09:00"
        starts_at = f"{date}T{time}:00" if date else None
        if not starts_at:
            raise HTTPException(400, "appointment has no extractable date")
        skill_args = {
            "title":         payload.get("subject") or "Appointment",
            "starts_at":     starts_at,
            "notes":         f"With {payload.get('with', 'someone')}. Auto-imported from email (notif #{notification_id})",
        }
        result = await _run_skill("add_calendar_event", skill_args, user)
    else:
        raise HTTPException(400, f"unknown proposal category: {category}")

    notif.mark_read(user["id"], [notification_id])
    return {"ok": True, "skill_result": result}


class SpamMarkIn(BaseModel):
    # When False, only the exact sender address is blocked. When True,
    # the entire domain is blocked too (covers all addresses @example.com).
    # Defaults to False — domain blocking is the explicit, riskier choice.
    block_domain: bool = False


@router.post("/{notification_id}/spam")
def mark_proposal_spam(
    notification_id: int,
    payload: SpamMarkIn,
    user: dict = Depends(current_user),
):
    """Mark a bill/appointment proposal as spam:
      1. Add the sender to the user's email_blocklist (and the domain
         too if requested) so future mail from them doesn't notify.
      2. Try to IMAP-MOVE the source message to the Junk folder (best
         effort — provider's own spam learning kicks in for free once
         it's there).
      3. Mark the notification read so the bell clears it.

    Returns the list of blocklist entries that got created so the UI
    can show a meaningful confirmation toast.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, kind, payload_json FROM notifications "
            "WHERE id = ? AND user_id = ?",
            (notification_id, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "notification not found")
    if row["kind"] != "email_proposal":
        raise HTTPException(400, f"notification kind '{row['kind']}' is not spammable")

    try:
        npayload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        raise HTTPException(500, "notification payload corrupted")

    sender_address = (npayload.get("from_email") or "").strip()
    message_id     = npayload.get("message_id")
    if not sender_address:
        # Older notifications (created before payload carried from_email)
        # don't have a sender to block. Just drop the notification —
        # that's the least-surprising thing we can do.
        notif.mark_read(user["id"], [notification_id])
        return {
            "ok":            True,
            "blocked":       [],
            "moved_to_junk": False,
            "note":          "notification had no sender address — "
                             "dropped from the bell but no blocklist entry created",
        }

    blocked: list[dict] = []
    sender_id = email_blocklist.add_sender(
        user["id"], sender_address,
        reason=f"marked spam via notification #{notification_id}",
    )
    blocked.append({"kind": "sender", "value": sender_address, "id": sender_id})

    if payload.block_domain:
        dom = email_blocklist.domain_of(sender_address)
        if dom:
            domain_id = email_blocklist.add_domain(
                user["id"], dom,
                reason=f"marked spam via notification #{notification_id}",
            )
            blocked.append({"kind": "domain", "value": dom, "id": domain_id})

    # Best-effort IMAP move. Failures here don't fail the request —
    # the blocklist is the load-bearing part. If the move fails the
    # user just sees the mail still in their inbox, but Yorik won't
    # nag them again.
    moved = False
    if isinstance(message_id, int) and message_id > 0:
        try:
            from . import email_actions
            moved = email_actions.move_to_junk(message_id, user["id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("move_to_junk failed for msg %s: %s", message_id, exc)

    notif.mark_read(user["id"], [notification_id])
    return {"ok": True, "blocked": blocked, "moved_to_junk": moved}


async def _run_skill(skill_name: str, args: dict, user: dict) -> dict:
    from .skills.registry import get_registry, SkillContext
    reg = get_registry()
    ctx = SkillContext(reg, role=user.get("role", "admin"), user_id=user["id"])
    return await reg.invoke(skill_name, ctx=ctx, **args)

