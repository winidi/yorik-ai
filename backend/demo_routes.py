"""REST endpoints for the demo-data feature.

Three endpoints, all admin-only:
  GET    /api/demo/status   — {seeded, seeded_at, counts, total}
  POST   /api/demo/seed     — install one-shot. 409 if already seeded.
  DELETE /api/demo          — remove tracked entries; returns deletion counts.

The Home screen uses /status to decide whether to show the "Try with
example data" panel (only when no demo and no real data is present).
Settings → Privacy shows a "Remove demo data" button when seeded.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from . import demo_data
from .auth_sessions import current_user

router = APIRouter(prefix="/api/demo", tags=["demo"])


def _require_admin(user: dict) -> None:
    if (user or {}).get("role") not in ("admin", "platform_admin"):
        raise HTTPException(403, "admin only")


@router.get("/status")
def demo_status(user: dict = Depends(current_user)) -> dict:
    """No admin gate — anyone can ASK if demo is loaded (it informs
    UI placement). Only the mutations are admin-restricted."""
    return demo_data.summary()


@router.post("/seed")
def demo_seed(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    if demo_data.is_seeded():
        raise HTTPException(409, "demo data already loaded — remove it first")
    inserted = demo_data.seed_all(user_id=user["id"])
    return {"ok": True, "inserted": {k: len(v) for k, v in inserted.items()}}


@router.delete("")
def demo_remove(user: dict = Depends(current_user)) -> dict:
    _require_admin(user)
    if not demo_data.is_seeded():
        # 200 + zero counts rather than 404 — idempotent feels right
        # here (UI shouldn't have to special-case "nothing to delete").
        return {"ok": True, "deleted": {}}
    deleted = demo_data.remove_all()
    return {"ok": True, "deleted": deleted}
