"""Sharing — what of mine another household member may see.

    GET /api/sharing                      members + what I share with each + what they share with me
    PUT /api/sharing/{member_user_id}     {"areas": ["tasks","calendar"], "level": "read"|"write"}
                                          areas [] removes the share
    Both accept ?owner=<user_id> so an admin can set sharing for a
    child/restricted account they manage.

Implemented as a membership in the owner's personal space with a scope
(space_members.scope). "Read" means seeing, "write" means editing too.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .database import get_conn

router = APIRouter(prefix="/api/sharing", tags=["sharing"])

AREAS = ("tasks", "calendar", "contacts", "documents")


def _current_user():
    from .auth_sessions import current_user
    return current_user


def _owner_for(user: Dict[str, Any], owner: Optional[str]) -> str:
    """Whose sharing is being edited. Yourself, or — for admins — a
    restricted/child account."""
    if not owner or str(owner) == str(user["id"]):
        return str(user["id"])
    if (user.get("role") or "").lower() not in ("admin", "platform_admin"):
        raise HTTPException(status_code=403, detail="you can only change your own sharing")
    with get_conn() as conn:
        row = conn.execute("SELECT role FROM user_profiles WHERE id = ?", (owner,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="no such user")
    if (row["role"] or "").lower() not in ("restricted", "child", "viewer", "employee"):
        raise HTTPException(status_code=403, detail="admins manage sharing only for restricted accounts")
    return str(owner)


def _personal_space(owner_id: str) -> int:
    from . import spaces as _sp
    sid = _sp.personal_space_id(owner_id)
    if sid is None:
        with get_conn() as conn:
            row = conn.execute("SELECT name FROM user_profiles WHERE id = ?", (owner_id,)).fetchone()
        sid = _sp.ensure_personal_space(owner_id, (row["name"] if row else None) or "Personal")
    return int(sid)


def _parse_scope(scope: Optional[str]) -> List[str]:
    if not scope:
        return list(AREAS)
    return [a for a in (x.strip() for x in scope.split(",")) if a in AREAS]


@router.get("")
def sharing(owner: Optional[str] = Query(default=None), user: Dict[str, Any] = Depends(_current_user())):
    owner_id = _owner_for(user, owner)
    my_space = _personal_space(owner_id)
    with get_conn() as conn:
        members = [dict(r) for r in conn.execute(
            "SELECT id, name, role FROM user_profiles WHERE disabled = 0 AND id <> ? ORDER BY name", (owner_id,),
        ).fetchall()]
        given = {str(r["user_id"]): r for r in conn.execute(
            "SELECT user_id, level, scope FROM space_members WHERE space_id = ?", (my_space,),
        ).fetchall()}
        received = {}
        for r in conn.execute(
            "SELECT s.owner_user_id, m.level, m.scope FROM space_members m JOIN spaces s ON s.id = m.space_id "
            "WHERE m.user_id = ? AND s.kind = 'personal'", (owner_id,),
        ).fetchall():
            received[str(r["owner_user_id"])] = r
    out = []
    for m in members:
        g = given.get(str(m["id"]))
        rcv = received.get(str(m["id"]))
        out.append({
            "user_id": str(m["id"]), "name": m["name"], "role": m["role"],
            "i_share": {"areas": _parse_scope(g["scope"]), "level": g["level"]} if g else {"areas": [], "level": None},
            "shares_with_me": {"areas": _parse_scope(rcv["scope"]), "level": rcv["level"]} if rcv else {"areas": [], "level": None},
        })
    return {"owner_id": owner_id, "areas": list(AREAS), "members": out}


class ShareIn(BaseModel):
    areas: List[str]
    level: Literal["read", "write"] = "read"


@router.put("/{member_user_id}")
def set_sharing(member_user_id: str, body: ShareIn, owner: Optional[str] = Query(default=None),
                user: Dict[str, Any] = Depends(_current_user())):
    if user.get("auth") == "api_token":
        raise HTTPException(status_code=403, detail="log in to change sharing")
    owner_id = _owner_for(user, owner)
    if str(member_user_id) == owner_id:
        raise HTTPException(status_code=400, detail="that is you")
    areas = [a for a in body.areas if a in AREAS]
    if len(areas) != len(body.areas):
        raise HTTPException(status_code=400, detail=f"areas must be among {list(AREAS)}")
    my_space = _personal_space(owner_id)
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM user_profiles WHERE id = ? AND disabled = 0", (member_user_id,)).fetchone():
            raise HTTPException(status_code=404, detail="no such member")
        if not areas:
            conn.execute("DELETE FROM space_members WHERE space_id = ? AND user_id = ?", (my_space, member_user_id))
        else:
            scope = None if set(areas) == set(AREAS) else ",".join(a for a in AREAS if a in areas)
            conn.execute(
                "INSERT INTO space_members (space_id, user_id, level, scope, added_by_user_id) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (space_id, user_id) DO UPDATE SET level = EXCLUDED.level, scope = EXCLUDED.scope, "
                "added_by_user_id = EXCLUDED.added_by_user_id",
                (my_space, member_user_id, body.level, scope, user["id"]),
            )
        conn.commit()
    return {"ok": True, "owner_id": owner_id, "member_user_id": member_user_id, "areas": areas,
            "level": body.level if areas else None}
