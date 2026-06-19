"""Phase B.5 — REST API for spaces + members + workspace settings.

Powers the Settings → Spaces UI: list spaces the user can see, manage
membership, change a space's name / kind, switch the workspace kind
(family / business) which drives default placement for new rows.

Admin-only writes; reads filtered to spaces the caller can see.
Personal spaces are read-only (can't rename / delete / change members);
they're tied 1:1 to their owner and reflect ownership, not configuration.

Provisioning side-effects: every membership change fires the same
on_space_member_added / _removed hooks in backend.spaces that the
internal code path uses — so a Settings → Spaces edit syncs Paperless
groups + Immich albums identically to programmatic adds.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .auth_sessions import current_user, require_admin
from .database import conn_ctx, DEFAULT_DB_PATH as _DB
from . import spaces as _sp


router = APIRouter(prefix="/api", tags=["spaces"])


# ─── Models ─────────────────────────────────────────────────────────


class SpaceMemberOut(BaseModel):
    # Phase E: Yorik user ids are UUIDs (mirror auth.users.id).
    user_id:     str
    name:        str
    email:       str
    level:       Literal["read", "write", "admin"]
    added_at:    str
    paperless_user_id: Optional[int] = None
    immich_user_id:    Optional[str] = None


class SpaceOut(BaseModel):
    id:             int
    name:           str
    kind:           Literal["personal", "shared"]
    slug:           Optional[str] = None
    owner_user_id:  Optional[str] = None
    members_count:  int
    your_level:     Optional[Literal["read", "write", "admin"]] = None
    is_default_for: list[str] = Field(default_factory=list)


class SpaceDetailOut(SpaceOut):
    members: list[SpaceMemberOut]


class SpaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: Optional[str] = Field(default=None, max_length=40,
                                description="Short lowercase identifier (a-z, 0-9, -). Used by provisioning + URL deep-links. Optional — falls back to a slugified name.")


class SpacePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    color: Optional[str] = None
    icon: Optional[str] = None


class MemberAdd(BaseModel):
    user_id: str   # Phase E: UUID
    level:   Literal["read", "write", "admin"] = "write"


class MemberPatch(BaseModel):
    level: Literal["read", "write", "admin"]


class WorkspaceOut(BaseModel):
    id:    int
    name:  str
    kind:  Literal["family", "business"]
    owner_user_id: str   # Phase E: UUID


class WorkspacePatch(BaseModel):
    name: Optional[str] = None
    kind: Optional[Literal["family", "business"]] = None


class WorkspaceAdminCreate(BaseModel):
    """Admin user to seed a new workspace with. Same shape as
    UserCreate's required fields — kept inline so the create-workspace
    endpoint is one round-trip instead of two (create workspace,
    then create user, then wire ownership)."""
    name:     str = Field(min_length=1, max_length=80)
    email:    str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=6, max_length=200)


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["family", "business"] = "family"
    admin: WorkspaceAdminCreate


class WorkspaceCreateOut(WorkspaceOut):
    """Workspace plus the freshly-created admin user so callers can
    show "share these credentials" on the next screen without a
    second GET /api/users round-trip."""
    admin_user_id: str   # Phase E: UUID
    admin_email:   str
    shared_space_id: int
    provisioning:  dict[str, Any] = Field(default_factory=dict)


# ─── Workspace ──────────────────────────────────────────────────────


@router.get("/workspaces", response_model=list[WorkspaceOut])
def list_workspaces(
    user: dict[str, Any] = Depends(current_user),
) -> list[WorkspaceOut]:
    """List workspaces visible to the caller.

    - platform_admin → every workspace
    - admin / member / restricted → just the workspace they belong to
      (either as `workspaces.owner_user_id` or via space_members)

    Used by Settings → Workspaces and by the workspace-switcher UI.
    """
    role = (user.get("role") or "").lower()
    uid = user.get("id") or 0
    with conn_ctx(_DB) as c:
        if role == "platform_admin":
            rows = c.execute(
                "SELECT id, name, kind, owner_user_id FROM workspaces ORDER BY id"
            ).fetchall()
        else:
            # Workspaces owned by the user, plus workspaces of any space
            # they're a member of. UNION-DISTINCT handles double-counting.
            rows = c.execute(
                "SELECT w.id, w.name, w.kind, w.owner_user_id FROM workspaces w "
                "WHERE w.owner_user_id = ? "
                "  OR w.id IN ("
                "    SELECT s.workspace_id FROM spaces s "
                "    JOIN space_members sm ON sm.space_id = s.id "
                "    WHERE sm.user_id = ?"
                "  ) "
                "ORDER BY w.id",
                (uid, uid),
            ).fetchall()
    return [WorkspaceOut(**dict(r)) for r in rows]


@router.post("/workspaces", response_model=WorkspaceCreateOut, status_code=201)
def create_workspace(
    body: WorkspaceCreate,
    user: dict[str, Any] = Depends(require_admin),
) -> WorkspaceCreateOut:
    """Create a new workspace + its initial admin user, atomically.

    Only `platform_admin` may create workspaces — workspace admins are
    scoped to their own and can't spawn siblings.

    On success:
      1. user_profiles row for the new workspace admin (role='admin')
      2. workspaces row with `owner_user_id` = new admin
      3. Personal space for the new admin
      4. Shared space for the workspace
      5. Adds new admin to the Shared space at level='admin'
      6. Best-effort provisioning of Paperless + Immich for the new admin
         (as a non-superuser — workspace admins are scoped, not god-level)

    Returns the workspace + the new admin user so the calling UI can
    surface "share these credentials" on the next screen.
    """
    if (user.get("role") or "").lower() != "platform_admin":
        raise HTTPException(403, "only platform_admin can create workspaces")

    from . import auth_sessions as _auth
    import re

    email = body.admin.email.strip().lower()
    if _auth.get_user_by_email(email):
        raise HTTPException(409, "admin email already in use")

    pw_hash = _auth.hash_password(body.admin.password)

    with conn_ctx(_DB) as conn:
        # 1. Create the admin user row.
        admin_cur = conn.execute(
            "INSERT INTO user_profiles (name, email, role, password_hash, password_set_at) "
            "VALUES (?, ?, 'admin', ?, datetime('now'))",
            (body.admin.name, email, pw_hash),
        )
        admin_uid = int(admin_cur.lastrowid)

        # 2. Workspace row.
        ws_cur = conn.execute(
            "INSERT INTO workspaces (name, kind, owner_user_id) VALUES (?, ?, ?)",
            (body.name, body.kind, admin_uid),
        )
        workspace_id = int(ws_cur.lastrowid)

        # 3 + 4. Personal + Shared spaces in the new workspace.
        slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:40] or f"ws{workspace_id}"
        personal_cur = conn.execute(
            "INSERT INTO spaces (workspace_id, name, kind, slug, owner_user_id) "
            "VALUES (?, ?, 'personal', NULL, ?)",
            (workspace_id, f"{body.admin.name}'s space", admin_uid),
        )
        personal_space_id = int(personal_cur.lastrowid)
        shared_cur = conn.execute(
            "INSERT INTO spaces (workspace_id, name, kind, slug, owner_user_id) "
            "VALUES (?, 'Shared', 'shared', ?, ?)",
            (workspace_id, slug, admin_uid),
        )
        shared_space_id = int(shared_cur.lastrowid)

        # 5. Make the admin a write-level member of their Shared space.
        # spaces.user_space_level treats workspace owners as 'admin' on every
        # space in their workspace, so a row in space_members is technically
        # redundant — but the Settings UI lists members from this table, so
        # the admin should appear there explicitly for parity with member adds.
        conn.execute(
            "INSERT INTO space_members (space_id, user_id, level) VALUES (?, ?, 'admin')",
            (shared_space_id, admin_uid),
        )
        conn.commit()

    # 6. Best-effort connector provisioning. Failures are surfaced in
    # the response but don't roll back the workspace — operator can
    # re-run via /api/users/{id}/provision/{service} later.
    provisioning: dict[str, Any] = {}
    for service, fn in (
        ("paperless", "provision_paperless"),
        ("immich",    "provision_immich"),
    ):
        try:
            from . import external_users as _ex
            getattr(_ex, fn)(
                admin_uid, body.admin.name, email, body.admin.password,
                is_admin=False,
            )
            provisioning[service] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "not configured" in msg.lower():
                provisioning[service] = {"ok": False, "skipped": True, "reason": msg}
            else:
                provisioning[service] = {"ok": False, "error": msg}

    return WorkspaceCreateOut(
        id=workspace_id,
        name=body.name,
        kind=body.kind,
        owner_user_id=admin_uid,
        admin_user_id=admin_uid,
        admin_email=email,
        shared_space_id=shared_space_id,
        provisioning=provisioning,
    )


@router.get("/workspaces/current", response_model=WorkspaceOut)
def get_current_workspace(
    user: dict[str, Any] = Depends(current_user),  # noqa: ARG001
) -> WorkspaceOut:
    """Single workspace per install today. Returned as-is so the UI can
    show "Family / Business" toggle in Settings."""
    with conn_ctx(_DB) as c:
        row = c.execute(
            "SELECT id, name, kind, owner_user_id FROM workspaces LIMIT 1"
        ).fetchone()
    if not row:
        raise HTTPException(404, "no workspace yet — migration 036 not applied?")
    return WorkspaceOut(**dict(row))


@router.patch("/workspaces/current", response_model=WorkspaceOut)
def patch_current_workspace(
    body: WorkspacePatch,
    user: dict[str, Any] = Depends(require_admin),  # noqa: ARG001
) -> WorkspaceOut:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return get_current_workspace(user)
    sets = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values())
    with conn_ctx(_DB) as c:
        c.execute(f"UPDATE workspaces SET {sets} WHERE id = (SELECT id FROM workspaces LIMIT 1)", params)
        row = c.execute(
            "SELECT id, name, kind, owner_user_id FROM workspaces LIMIT 1"
        ).fetchone()
    return WorkspaceOut(**dict(row))


# ─── Spaces ─────────────────────────────────────────────────────────


@router.get("/spaces", response_model=list[SpaceOut])
def list_spaces(
    user: dict[str, Any] = Depends(current_user),
) -> list[SpaceOut]:
    role = user.get("role")
    uid = user["id"]
    visible_ids = set(_sp.user_visible_space_ids(uid, role))
    out: list[SpaceOut] = []
    with conn_ctx(_DB) as c:
        for row in c.execute(
            "SELECT id, name, kind, slug, owner_user_id FROM spaces "
            "ORDER BY kind DESC, id ASC"
        ).fetchall():
            sid = int(row["id"])
            if sid not in visible_ids:
                continue
            count = c.execute(
                "SELECT COUNT(*) AS n FROM space_members WHERE space_id=?",
                (sid,),
            ).fetchone()["n"]
            level = _sp.user_space_level(uid, sid, role)
            out.append(SpaceOut(
                id=sid, name=row["name"], kind=row["kind"], slug=row["slug"],
                owner_user_id=row["owner_user_id"], members_count=count,
                your_level=level,
            ))
    return out


@router.get("/spaces/{space_id}", response_model=SpaceDetailOut)
def get_space(
    space_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> SpaceDetailOut:
    role = user.get("role")
    uid = user["id"]
    if space_id not in _sp.user_visible_space_ids(uid, role):
        raise HTTPException(404, "space not found")
    with conn_ctx(_DB) as c:
        row = c.execute(
            "SELECT id, name, kind, slug, owner_user_id FROM spaces WHERE id=?",
            (space_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "space not found")
        members_rows = c.execute(
            "SELECT sm.user_id, u.name, u.email, sm.level, sm.added_at, "
            "       u.paperless_user_id, u.immich_user_id "
            "FROM space_members sm JOIN user_profiles u ON u.id = sm.user_id "
            "WHERE sm.space_id=? ORDER BY u.name COLLATE NOCASE",
            (space_id,),
        ).fetchall()
    # Personal-space owner is an implicit admin without a space_members
    # row. Surface them explicitly so the UI can render the "owner" badge.
    members: list[SpaceMemberOut] = []
    if row["kind"] == "personal" and row["owner_user_id"]:
        with conn_ctx(_DB) as c:
            owner = c.execute(
                "SELECT id AS user_id, name, email, paperless_user_id, immich_user_id "
                "FROM user_profiles WHERE id=?",
                (row["owner_user_id"],),
            ).fetchone()
        if owner:
            members.append(SpaceMemberOut(
                user_id=owner["user_id"], name=owner["name"], email=owner["email"],
                level="admin", added_at="(owner)",
                paperless_user_id=owner["paperless_user_id"],
                immich_user_id=owner["immich_user_id"],
            ))
    for m in members_rows:
        members.append(SpaceMemberOut(**dict(m)))
    return SpaceDetailOut(
        id=row["id"], name=row["name"], kind=row["kind"], slug=row["slug"],
        owner_user_id=row["owner_user_id"], members_count=len(members),
        your_level=_sp.user_space_level(uid, space_id, role),
        members=members,
    )


@router.post("/spaces", response_model=SpaceDetailOut, status_code=201)
def create_space(
    body: SpaceCreate,
    user: dict[str, Any] = Depends(require_admin),
) -> SpaceDetailOut:
    slug = (body.slug or _slugify(body.name)).lower()
    if not slug:
        raise HTTPException(400, "slug required")
    with conn_ctx(_DB) as c:
        ws = c.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
        if not ws:
            raise HTTPException(409, "no workspace yet")
        dup = c.execute(
            "SELECT id FROM spaces WHERE workspace_id=? AND slug=?",
            (ws["id"], slug),
        ).fetchone()
        if dup:
            raise HTTPException(409, f"space with slug {slug!r} already exists")
        cur = c.execute(
            "INSERT INTO spaces (workspace_id, name, kind, slug) VALUES (?, ?, 'shared', ?)",
            (ws["id"], body.name, slug),
        )
        space_id = int(cur.lastrowid)
        c.commit()
    return get_space(space_id, user)


@router.patch("/spaces/{space_id}", response_model=SpaceDetailOut)
def patch_space(
    space_id: int,
    body: SpacePatch,
    user: dict[str, Any] = Depends(require_admin),
) -> SpaceDetailOut:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return get_space(space_id, user)
    with conn_ctx(_DB) as c:
        row = c.execute("SELECT kind FROM spaces WHERE id=?", (space_id,)).fetchone()
        if not row:
            raise HTTPException(404, "space not found")
        if row["kind"] == "personal":
            raise HTTPException(
                400,
                "personal spaces can't be renamed — they're tied 1:1 to their owner",
            )
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE spaces SET {sets} WHERE id=?", list(fields.values()) + [space_id])
    return get_space(space_id, user)


@router.delete("/spaces/{space_id}", status_code=204, response_class=Response)
def delete_space(
    space_id: int,
    user: dict[str, Any] = Depends(require_admin),  # noqa: ARG001
) -> Response:
    with conn_ctx(_DB) as c:
        row = c.execute("SELECT kind, slug FROM spaces WHERE id=?", (space_id,)).fetchone()
        if not row:
            raise HTTPException(404, "space not found")
        if row["kind"] == "personal":
            raise HTTPException(
                400,
                "personal spaces can't be deleted — delete the user instead to remove their personal space",
            )
        if row["slug"] in ("household", "finance"):
            raise HTTPException(
                400,
                f"the '{row['slug']}' space is reserved — can't delete",
            )
        # Refuse if any domain rows still live there.
        for table in ("tasks", "contacts", "bills", "agent_conversations"):
            n = c.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE space_id=?", (space_id,),
            ).fetchone()["n"]
            if n:
                raise HTTPException(
                    409,
                    f"{n} {table} row(s) still in this space — move or delete them first",
                )
        n = c.execute(
            "SELECT COUNT(*) AS n FROM calendars WHERE space_id=?", (space_id,),
        ).fetchone()["n"]
        if n:
            raise HTTPException(
                409,
                f"{n} calendar(s) still in this space — move or delete them first",
            )
        c.execute("DELETE FROM spaces WHERE id=?", (space_id,))
        c.commit()
    return Response(status_code=204)


# ─── Members ────────────────────────────────────────────────────────


@router.post("/spaces/{space_id}/members", response_model=SpaceDetailOut)
def add_member(
    space_id: int,
    body: MemberAdd,
    user: dict[str, Any] = Depends(require_admin),
) -> SpaceDetailOut:
    with conn_ctx(_DB) as c:
        space = c.execute("SELECT kind FROM spaces WHERE id=?", (space_id,)).fetchone()
        if not space:
            raise HTTPException(404, "space not found")
        if space["kind"] == "personal":
            raise HTTPException(400, "personal spaces have a fixed single member (the owner)")
        target = c.execute("SELECT id FROM user_profiles WHERE id=?", (body.user_id,)).fetchone()
        if not target:
            raise HTTPException(404, f"user {body.user_id} not found")
        c.execute(
            "INSERT OR REPLACE INTO space_members "
            "(space_id, user_id, level, added_by_user_id) VALUES (?, ?, ?, ?)",
            (space_id, body.user_id, body.level, user["id"]),
        )
        c.commit()
    _sp.on_space_member_added(space_id, body.user_id, body.level)
    return get_space(space_id, user)


@router.patch("/spaces/{space_id}/members/{member_user_id}", response_model=SpaceDetailOut)
def patch_member(
    space_id: int,
    member_user_id: str,   # Phase E: UUID
    body: MemberPatch,
    user: dict[str, Any] = Depends(require_admin),
) -> SpaceDetailOut:
    with conn_ctx(_DB) as c:
        existing = c.execute(
            "SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
            (space_id, member_user_id),
        ).fetchone()
        if not existing:
            raise HTTPException(404, "not a member")
        c.execute(
            "UPDATE space_members SET level=? WHERE space_id=? AND user_id=?",
            (body.level, space_id, member_user_id),
        )
        c.commit()
    # Level change can shift Immich role (editor↔viewer); fire the hook.
    _sp.on_space_member_added(space_id, member_user_id, body.level)
    return get_space(space_id, user)


@router.delete("/spaces/{space_id}/members/{member_user_id}", status_code=204, response_class=Response)
def remove_member(
    space_id: int,
    member_user_id: str,   # Phase E: UUID
    user: dict[str, Any] = Depends(require_admin),  # noqa: ARG001
) -> Response:
    with conn_ctx(_DB) as c:
        existing = c.execute(
            "SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
            (space_id, member_user_id),
        ).fetchone()
        if existing:
            c.execute(
                "DELETE FROM space_members WHERE space_id=? AND user_id=?",
                (space_id, member_user_id),
            )
            c.commit()
            _sp.on_space_member_removed(space_id, member_user_id)
    return Response(status_code=204)


def _slugify(s: str) -> str:
    """Lowercase, replace non-alphanumeric with hyphen, strip edges."""
    import re
    out = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return out[:40]
