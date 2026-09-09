"""Contact identity routes — proposals a human decides, merges they can undo.

    GET  /api/contacts/proposals                 pending suggestions with both contacts
    POST /api/contacts/proposals/{id}/accept     {keep_id?}  apply (merge or add channel)
    POST /api/contacts/proposals/{id}/reject
    GET  /api/contacts/merges                    recent merges
    POST /api/contacts/merges/{id}/undo
    POST /api/contacts/merge                     {keep_id, drop_id}  manual merge
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import contact_identity as I

router = APIRouter(prefix="/api/contacts", tags=["contacts-identity"])


def _current_user():
    from .auth_sessions import current_user
    return current_user


def _may_edit(user: Dict[str, Any]) -> None:
    if (user.get("role") or "").lower() not in ("admin", "platform_admin", "member"):
        raise HTTPException(status_code=403, detail="members and admins only")


class AcceptIn(BaseModel):
    keep_id: Optional[int] = None


class MergeIn(BaseModel):
    keep_id: int
    drop_id: int
    reason: str = "manual"


@router.get("/proposals")
def proposals(status: str = "pending", user: Dict[str, Any] = Depends(_current_user())):
    _may_edit(user)
    if status not in ("pending", "accepted", "rejected"):
        raise HTTPException(status_code=400, detail="status must be pending|accepted|rejected")
    return {"proposals": I.list_proposals(status)}


@router.post("/proposals/{pid}/accept")
def accept(pid: int, body: AcceptIn = AcceptIn(), user: Dict[str, Any] = Depends(_current_user())):
    _may_edit(user)
    try:
        return I.accept_proposal(pid, decided_by=str(user.get("id")), keep_id=body.keep_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/proposals/{pid}/reject")
def reject(pid: int, user: Dict[str, Any] = Depends(_current_user())):
    _may_edit(user)
    try:
        I.reject_proposal(pid, decided_by=str(user.get("id")))
    except KeyError:
        raise HTTPException(status_code=404, detail="proposal not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@router.get("/merges")
def merges(user: Dict[str, Any] = Depends(_current_user())):
    _may_edit(user)
    return {"merges": I.list_merges()}


@router.post("/merges/{mid}/undo")
def undo(mid: int, user: Dict[str, Any] = Depends(_current_user())):
    _may_edit(user)
    try:
        return I.unmerge(mid)
    except KeyError:
        raise HTTPException(status_code=404, detail="merge not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/merge")
def merge(body: MergeIn, user: Dict[str, Any] = Depends(_current_user())):
    _may_edit(user)
    try:
        return {"ok": True, "merge": I.merge(body.keep_id, body.drop_id, reason=body.reason,
                                             decided_by=str(user.get("id")))}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
