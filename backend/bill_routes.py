"""REST for the Finance → Bills tab: list, add, edit, pay, delete.

Reads are filtered by spaces.row_filter (own bills + the Finance space);
writes go through spaces.can_write_row. Children (restricted) have no
bills at all (auth.ROLE_TABLES). The chat uses the bill skills instead,
so undo and confirm hook in there; these endpoints write at once
because the person is in the UI acting with intent.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from . import bills, spaces
from .auth import require_role, require_write
from .auth_sessions import current_user

log = logging.getLogger("yorik.bills")

router = APIRouter(prefix="/api/bills", tags=["bills"])


def _role(user: dict) -> str:
    return str(user.get("role") or "member")


def _writable(bill_id: int, user: dict) -> Dict[str, Any]:
    require_role(_role(user), "bills")
    require_write(_role(user))
    row = bills.get_bill(bill_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"bill {bill_id} not found")
    if not spaces.can_write_row(user["id"], _role(user), "bills", row):
        raise HTTPException(status_code=403, detail="not your bill")
    return row


@router.get("")
def list_bills(status: str = Query("open", pattern="^(open|paid|all)$"),
               user: dict = Depends(current_user)) -> List[Dict[str, Any]]:
    require_role(_role(user), "bills")
    # Before showing the open ones, let the bank tick off what it paid.
    try:
        bills.settle_with_bank(user["id"])
    except Exception as exc:  # noqa: BLE001
        log.debug("bills: settle on list failed: %s", exc)
    return bills.list_bills(user["id"], _role(user), status)


@router.get("/summary")
def bill_summary(user: dict = Depends(current_user)) -> Dict[str, Any]:
    require_role(_role(user), "bills")
    return bills.summary(user["id"], _role(user))


class BillIn(BaseModel):
    name: str
    amount: float
    currency: Optional[str] = None
    due_date: Optional[str] = None
    payee: Optional[str] = None
    number: Optional[str] = None
    recurring: Optional[str] = None
    notes: Optional[str] = None


@router.post("", status_code=201)
def create_bill(body: BillIn, user: dict = Depends(current_user)) -> Dict[str, Any]:
    require_role(_role(user), "bills")
    require_write(_role(user))
    try:
        return bills.create_bill(user_id=user["id"], source="manual", **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class BillPatch(BaseModel):
    name: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    due_date: Optional[str] = None
    payee: Optional[str] = None
    number: Optional[str] = None
    recurring: Optional[str] = None
    notes: Optional[str] = None
    paid: Optional[bool] = None


@router.patch("/{bill_id}")
def patch_bill(bill_id: int, body: BillPatch, user: dict = Depends(current_user)) -> Dict[str, Any]:
    _writable(bill_id, user)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        paid = fields.pop("paid", None)
        row = bills.update_bill(bill_id, **fields) if fields else bills.get_bill(bill_id)
        if paid is not None:
            row = bills.set_paid(bill_id, bool(paid))
        return row  # type: ignore[return-value]
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{bill_id}/paid")
def mark_paid(bill_id: int, user: dict = Depends(current_user)) -> Dict[str, Any]:
    _writable(bill_id, user)
    return bills.set_paid(bill_id, True)


@router.post("/{bill_id}/unpaid")
def mark_unpaid(bill_id: int, user: dict = Depends(current_user)) -> Dict[str, Any]:
    _writable(bill_id, user)
    return bills.set_paid(bill_id, False)


@router.delete("/{bill_id}")
def delete_bill(bill_id: int, user: dict = Depends(current_user)) -> Dict[str, Any]:
    _writable(bill_id, user)
    return {"ok": bills.delete_bill(bill_id), "bill_id": bill_id}
