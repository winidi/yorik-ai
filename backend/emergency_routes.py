"""Routes for the emergency access — see emergency.py."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import auth_sessions as _auth
from . import emergency

router = APIRouter(prefix="/api/emergency-access", tags=["emergency"])


class _StartBody(BaseModel):
    reason: str
    hours: int = 24
    password: str


@router.get("")
def status(user: Dict[str, Any] = Depends(_auth.current_user)) -> Dict[str, Any]:
    """The caller's own running access and the household log. Children
    get neither (the log names adults and their reasons)."""
    if (user.get("role") or "").lower() == "restricted":
        raise HTTPException(403, "emergency access is for the adults of the household")
    return {"mine": emergency.active(user["id"]), "history": emergency.history(),
            "max_hours": emergency.MAX_HOURS, "min_reason_chars": emergency.MIN_REASON_CHARS}


@router.post("")
def start(body: _StartBody, user: Dict[str, Any] = Depends(_auth.current_user)) -> Dict[str, Any]:
    try:
        return emergency.start(user, reason=body.reason, hours=body.hours, password=body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/{access_id}")
def end(access_id: int, user: Dict[str, Any] = Depends(_auth.current_user)) -> Dict[str, Any]:
    if not emergency.end(user, access_id):
        raise HTTPException(404, "no running emergency access of yours with that id")
    return {"ok": True}
