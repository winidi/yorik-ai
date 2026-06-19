"""Backup REST API — config get/set, run-now, history, target-check."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth_sessions import require_admin
from . import backup

router = APIRouter(prefix="/api/backup", tags=["backup"])


class ConfigPatch(BaseModel):
    target_path:       Optional[str] = None
    schedule:          Optional[str] = None  # "HH:MM" local, "" disables
    include_photos:    Optional[bool] = None
    include_paperless: Optional[bool] = None
    include_whatsapp:  Optional[bool] = None
    retain_count:      Optional[int] = Field(default=None, ge=1, le=365)
    passphrase:        Optional[str] = None  # set once, never returned


@router.get("/config", dependencies=[Depends(require_admin)])
def get_config():
    return backup.get_config()


@router.patch("/config", dependencies=[Depends(require_admin)])
def patch_config(body: ConfigPatch):
    try:
        return backup.set_config(
            target_path=body.target_path,
            schedule=body.schedule,
            include_photos=body.include_photos,
            include_paperless=body.include_paperless,
            include_whatsapp=body.include_whatsapp,
            retain_count=body.retain_count,
            passphrase=body.passphrase,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/status", dependencies=[Depends(require_admin)])
def status():
    """One-call dashboard — config + target availability + recent runs."""
    cfg = backup.get_config()
    return {
        "config":     cfg,
        "target":     backup.target_available(cfg["target_path"]),
        "history":    backup.list_history(limit=10),
        "snapshots":  backup.list_snapshots_on_target(),
    }


@router.post("/run", dependencies=[Depends(require_admin)])
async def run_now():
    return await backup.run_backup()
