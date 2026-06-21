"""HTTP API for the suggestion engine.

Read endpoints:
  GET  /api/suggestions?source_kind=email&source_id=<id>
       → list of pending suggestions for one source message.
  GET  /api/suggestions/settings
       → user's master toggle + per-source dict.

Write endpoints:
  POST /api/suggestions/<id>/accept   {payload? (edited)}
       → runs the type's handler, marks status='accepted'/'edited',
         returns {ok, result}.
  POST /api/suggestions/<id>/dismiss  {reason?}
       → marks status='dismissed'. No side effects.
  POST /api/suggestions/settings      {suggestions_enabled?, sources?}
       → updates the master / per-source toggles.

Contact toggles live on the contacts module (/api/contacts/.../yorik-assist).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .auth_sessions import current_user
from .database import get_conn
from .suggestions import registry as _reg
from .suggestions.registry import HandlerContext

log = logging.getLogger("yorik.suggestions.routes")

router = APIRouter(prefix="/api/suggestions", tags=["suggestions"])


def _row_to_suggestion(r: dict) -> dict[str, Any]:
    """Flatten a suggestions row + JSON cols into the API shape."""
    payload = r.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    resolved = r.get("resolved_payload_json")
    if isinstance(resolved, str):
        try:
            resolved = json.loads(resolved)
        except json.JSONDecodeError:
            resolved = None
    return {
        "id":         r["id"],
        "type":       r["type"],
        "payload":    payload or {},
        "confidence": r.get("confidence") or "medium",
        "reason":     r.get("reason") or "",
        "status":     r.get("status") or "pending",
        "resolved":   resolved,
        "created_at": r.get("created_at"),
    }


@router.get("")
def list_suggestions(
    source_kind: str = Query(...),
    source_id:   int = Query(...),
    include:     str = Query("pending", description="csv of statuses: pending,accepted,edited,dismissed"),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """All suggestions for one source message. Defaults to pending —
    the Reader pane only wants actionable cards."""
    allowed = {s.strip() for s in include.split(",") if s.strip()}
    if not allowed:
        allowed = {"pending"}
    placeholders = ",".join(["?"] * len(allowed))
    with get_conn() as c:
        rows = c.execute(
            f"SELECT s.id, s.type, s.payload_json, s.confidence, s.reason, "
            f"       s.status, s.resolved_payload_json, s.created_at "
            f"FROM suggestions s "
            f"JOIN suggestion_runs r ON r.id = s.run_id "
            f"WHERE s.owner_user_id=? AND r.source_kind=? AND r.source_id=? "
            f"  AND s.status IN ({placeholders}) "
            f"ORDER BY s.id DESC",
            (user["id"], source_kind, int(source_id), *allowed),
        ).fetchall()
        out_items = []
        for r in rows:
            d = dict(r)
            ev = c.execute(
                "SELECT id, kind, ref_id, ref_text, snippet "
                "FROM suggestion_evidence WHERE suggestion_id=? ORDER BY id",
                (d["id"],),
            ).fetchall()
            item = _row_to_suggestion(d)
            item["evidence"] = [dict(e) for e in ev]
            out_items.append(item)
    return {"items": out_items}


@router.post("/{suggestion_id}/accept")
async def accept_suggestion(
    suggestion_id: int,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    """Run the type's handler. Body may carry an edited `payload` to
    override what the LLM produced (user tweaks the draft before
    sending, edits the meeting time, etc.) — in that case the status
    becomes 'edited' rather than 'accepted'."""
    with get_conn() as c:
        srow = c.execute(
            "SELECT s.id, s.type, s.payload_json, s.status, "
            "       r.source_kind, r.source_id, r.contact_id "
            "FROM suggestions s JOIN suggestion_runs r ON r.id=s.run_id "
            "WHERE s.id=? AND s.owner_user_id=?",
            (suggestion_id, user["id"]),
        ).fetchone()
    if not srow:
        raise HTTPException(404, "suggestion not found")
    if srow["status"] != "pending":
        raise HTTPException(409, f"suggestion already {srow['status']}")

    stype = _reg.get_type(srow["type"])
    if not stype:
        raise HTTPException(409, f"unknown suggestion type {srow['type']!r}")

    edited_payload = body.get("payload")
    payload = edited_payload if isinstance(edited_payload, dict) else srow["payload_json"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}

    ctx = HandlerContext(
        owner_user_id=user["id"],
        user_role=user.get("role") or "admin",
        suggestion_id=suggestion_id,
        source_kind=srow["source_kind"],
        source_id=int(srow["source_id"]),
        contact_id=int(srow["contact_id"]) if srow["contact_id"] is not None else None,
    )
    try:
        result = await stype.handler(payload, ctx)
    except Exception as exc:  # noqa: BLE001
        log.exception("suggestion %s handler crashed: %s", suggestion_id, exc)
        raise HTTPException(500, f"handler crashed: {exc}")

    new_status = "edited" if edited_payload else "accepted"
    with get_conn() as c:
        c.execute(
            "UPDATE suggestions SET status=?, resolved_payload_json=?, "
            "  resolved_at=NOW() WHERE id=?",
            (new_status, json.dumps(result or {}), suggestion_id),
        )
        c.commit()
    return {"ok": True, "status": new_status, "result": result}


@router.post("/{suggestion_id}/dismiss")
def dismiss_suggestion(
    suggestion_id: int,
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    reason = (body.get("reason") or "").strip()[:200] or None
    with get_conn() as c:
        r = c.execute(
            "UPDATE suggestions SET status='dismissed', resolved_at=NOW(), "
            "  resolved_payload_json=COALESCE(?, resolved_payload_json) "
            "WHERE id=? AND owner_user_id=? AND status='pending' RETURNING id",
            (json.dumps({"reason": reason}) if reason else None,
             suggestion_id, user["id"]),
        ).fetchone()
        c.commit()
    if not r:
        raise HTTPException(404, "suggestion not found or already resolved")
    return {"ok": True}


@router.get("/settings")
def get_settings(user: dict = Depends(current_user)) -> dict[str, Any]:
    """Master toggle + per-source dict. Defaults: master=False,
    sources={"email": True} so the first time the user flips master
    on, email is already active."""
    with get_conn() as c:
        r = c.execute(
            "SELECT suggestions_enabled, suggestion_sources "
            "FROM user_profiles WHERE id=?",
            (user["id"],),
        ).fetchone()
    if not r:
        return {"suggestions_enabled": False, "sources": {"email": True}}
    sources = r["suggestion_sources"] or {"email": True}
    if isinstance(sources, str):
        try:
            sources = json.loads(sources)
        except json.JSONDecodeError:
            sources = {"email": True}
    return {
        "suggestions_enabled": bool(r["suggestions_enabled"]),
        "sources": sources,
    }


@router.post("/settings")
def update_settings(
    body: dict[str, Any] = Body(default_factory=dict),
    user: dict = Depends(current_user),
) -> dict[str, Any]:
    updates: list[str] = []
    args: list[Any] = []
    if "suggestions_enabled" in body:
        updates.append("suggestions_enabled = ?")
        args.append(bool(body["suggestions_enabled"]))
    if "sources" in body and isinstance(body["sources"], dict):
        clean = {str(k): bool(v) for k, v in body["sources"].items()}
        updates.append("suggestion_sources = ?")
        args.append(json.dumps(clean))
    if not updates:
        return get_settings(user)
    args.append(user["id"])
    with get_conn() as c:
        c.execute(
            f"UPDATE user_profiles SET {', '.join(updates)} WHERE id=?",
            tuple(args),
        )
        c.commit()
    return get_settings(user)
