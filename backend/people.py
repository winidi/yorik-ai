"""People — a colour and a photo per household member.

The family board, the calendar, the task columns and the kiosk sign-in
all show the same person the same way: their colour, their photo (or
initials on that colour). Colour lives on user_profiles.color; the
personal calendar follows it so events are person-coloured without
touching the calendar app. Photos are 256 px squares under
data/avatars/<user_id>.jpg, served to any signed-in session (a kiosk
session included) — a household member's face is not a secret inside
the household.
"""

from __future__ import annotations

import io
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .database import get_conn

log = logging.getLogger("yorik.people")

AVATAR_DIR = Path(os.getenv("HOMEOS_AVATAR_DIR", "data/avatars"))
AVATAR_PX = 256
MAX_UPLOAD = 12 * 1024 * 1024

# Warm, distinct, readable on white and as a fill behind white text.
PALETTE = ["#0f7c8a", "#e0486b", "#f0a020", "#6d5bd0", "#2f9e64", "#d9552b", "#2b6cb0", "#b5379a"]
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def avatar_path(user_id: str) -> Path:
    return AVATAR_DIR / f"{user_id}.jpg"


def has_avatar(user_id: str) -> bool:
    return avatar_path(str(user_id)).exists()


def avatar_url(user_id: str, avatar_at: Optional[str] = None) -> Optional[str]:
    if not has_avatar(user_id):
        return None
    v = re.sub(r"\D", "", avatar_at or "")[-10:] or "0"
    return f"/api/users/{user_id}/avatar?v={v}"


def default_color(user_id: str) -> str:
    """A stable colour for someone who never picked one: the palette,
    indexed by the order people joined the household."""
    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM user_profiles ORDER BY created_at, id").fetchall()
    ids = [str(r["id"]) for r in rows]
    idx = ids.index(str(user_id)) if str(user_id) in ids else 0
    return PALETTE[idx % len(PALETTE)]


def color_for(user_id: str, stored: Optional[str] = None) -> str:
    if stored and _HEX.match(stored):
        return stored.lower()
    return default_color(user_id)


def set_color(user_id: str, color: str) -> str:
    color = (color or "").strip().lower()
    if not _HEX.match(color):
        raise ValueError("colour must be #rrggbb")
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET color = ? WHERE id = ?", (color, user_id))
        # the personal calendar carries the person's colour
        conn.execute("UPDATE calendars SET color = ? WHERE owner_user_id = ? AND kind = 'personal' AND COALESCE(read_only, 0) = 0", (color, user_id))
        conn.commit()
    return color


def save_avatar(user_id: str, data: bytes) -> str:
    """Centre-crop to a square, shrink to AVATAR_PX, store as JPEG."""
    from PIL import Image, ImageOps
    if not data:
        raise ValueError("empty upload")
    if len(data) > MAX_UPLOAD:
        raise ValueError("image larger than 12 MB")
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"not an image ({type(exc).__name__})")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((AVATAR_PX, AVATAR_PX), Image.LANCZOS)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    tmp = avatar_path(user_id).with_suffix(".part")
    img.save(tmp, "JPEG", quality=88, optimize=True)
    tmp.replace(avatar_path(user_id))
    from datetime import datetime
    at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET avatar_at = ? WHERE id = ?", (at, user_id))
        conn.commit()
    return at


def remove_avatar(user_id: str) -> None:
    avatar_path(user_id).unlink(missing_ok=True)
    with get_conn() as conn:
        conn.execute("UPDATE user_profiles SET avatar_at = NULL WHERE id = ?", (user_id,))
        conn.commit()


def household(include_disabled: bool = False) -> List[Dict[str, Any]]:
    """Everyone with their look — what every people picker and every
    avatar in the UI is built from."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, COALESCE(first_name, '') AS first_name, role, color, avatar_at, disabled "
            "FROM user_profiles ORDER BY created_at, id").fetchall()
    out = []
    for i, r in enumerate(rows):
        if r["disabled"] and not include_disabled:
            continue
        uid = str(r["id"])
        out.append({
            "id": uid, "name": r["name"] or "(no name)", "first_name": r["first_name"] or "",
            "role": r["role"], "color": color_for(uid, r["color"]) if r["color"] else PALETTE[i % len(PALETTE)],
            "avatar_url": avatar_url(uid, r["avatar_at"]),
        })
    return out


def look_for(user_id: str) -> Dict[str, Any]:
    for p in household(include_disabled=True):
        if p["id"] == str(user_id):
            return {"color": p["color"], "avatar_url": p["avatar_url"], "palette": PALETTE}
    return {"color": PALETTE[0], "avatar_url": None, "palette": PALETTE}


# ─── routes ─────────────────────────────────────────────────────────

router = APIRouter(tags=["people"])


def _current_user():
    from .auth_sessions import current_user
    return current_user


class ColorIn(BaseModel):
    color: str


@router.get("/api/profile/look")
def get_look(user: Dict[str, Any] = Depends(_current_user())):
    return look_for(user["id"])


@router.patch("/api/profile/look")
def patch_look(body: ColorIn, user: Dict[str, Any] = Depends(_current_user())):
    if user.get("auth") == "api_token":
        raise HTTPException(status_code=403, detail="log in to change this")
    try:
        set_color(str(user["id"]), body.color)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return look_for(user["id"])


@router.post("/api/profile/avatar")
async def upload_avatar(image: UploadFile = File(...), user: Dict[str, Any] = Depends(_current_user())):
    if user.get("auth") == "api_token":
        raise HTTPException(status_code=403, detail="log in to change this")
    data = await image.read()
    try:
        save_avatar(str(user["id"]), data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return look_for(user["id"])


@router.delete("/api/profile/avatar")
def delete_avatar(user: Dict[str, Any] = Depends(_current_user())):
    remove_avatar(str(user["id"]))
    return look_for(user["id"])


@router.get("/api/users/{user_id}/avatar")
def serve_avatar(user_id: str, user: Dict[str, Any] = Depends(_current_user())):
    """Any signed-in session may see a household member's photo."""
    p = avatar_path(user_id)
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", user_id) or not p.exists():
        raise HTTPException(status_code=404, detail="no photo")
    return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/people/household")
def household_route(user: Dict[str, Any] = Depends(_current_user())):
    """Everyone in the household with colour and photo; no emails, no
    password state. What the UI builds avatars and pickers from."""
    return {"people": household()}
