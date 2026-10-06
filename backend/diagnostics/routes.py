"""The diagnostics API: consent, identity, the field list, local
pseudonym lookup, and the person's own reports.

Consent is the admin's, from a signed-in browser (require_admin_session:
not with an API token). Everyone may read the state — the chat needs it
to know whether a report can be offered. Reports are listed to their
own person; admins see them all, as on the privacy page's "what Yorik
has sent".
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.auth_sessions import current_user, require_admin, require_admin_session
from backend.database import get_conn

from . import collector_url, consent, install_id, reset_identity, set_consent
from . import pseudonyms, registry, scrub

log = logging.getLogger("yorik.diagnostics")

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


def _is_admin(user: Dict[str, Any]) -> bool:
    return (user.get("role") or "").lower() in ("admin", "platform_admin")


@router.get("/consent")
def get_consent(user: dict = Depends(current_user)) -> Dict[str, Any]:
    c = consent()
    return {**c, "is_admin": _is_admin(user), "llm_local": scrub.llm_is_local(),
            "collector_configured": bool(collector_url()),
            "install_id": install_id() if _is_admin(user) and c["asked"] else None}


class ConsentIn(BaseModel):
    counts: bool = False
    usage: bool = False
    errors: bool = False


@router.put("/consent")
def put_consent(body: ConsentIn, user: dict = Depends(require_admin_session)) -> Dict[str, Any]:
    return set_consent(counts=body.counts, usage=body.usage, errors=body.errors, user_id=user["id"])


@router.post("/identity/reset")
def post_reset(user: dict = Depends(require_admin_session)) -> Dict[str, Any]:
    return reset_identity(user_id=user["id"])


@router.get("/registry")
def get_registry(user: dict = Depends(current_user)) -> Dict[str, Any]:
    """Every field Yorik can send, with purpose and tier — the list the
    privacy page shows."""
    return {"schema": registry.FIELDS and 1, "fields": registry.describe()}


@router.get("/pseudonyms/resolve")
def resolve(token: str = Query(..., min_length=3, max_length=40),
            user: dict = Depends(require_admin_session)) -> Dict[str, Any]:
    """Who is person_7? Answered from this installation's own data only;
    nothing in clear is stored, the lookup is logged."""
    return {"token": token, "matches": pseudonyms.who_is(token, str(user["id"]))}


def _row(r) -> Dict[str, Any]:
    payload = r["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    summary = r["scrub_summary"]
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except ValueError:
            summary = None
    return {"id": str(r["id"]), "created_at": str(r["created_at"]), "kind": r["kind"], "trigger": r["trigger"],
            "status": r["status"], "attempts": r["attempts"], "last_error": r["last_error"],
            "sent_at": str(r["sent_at"]) if r["sent_at"] else None, "conversation_id": r["conversation_id"],
            "message_idx": r["message_idx"], "bytes": len(json.dumps(payload, ensure_ascii=False)),
            "payload": payload, "scrub_summary": summary}


@router.get("/reports")
def list_reports(limit: int = Query(50, ge=1, le=200), user: dict = Depends(current_user)) -> Dict[str, Any]:
    with get_conn() as conn:
        if _is_admin(user):
            rows = conn.execute("SELECT * FROM diag_reports ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM diag_reports WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                                (user["id"], limit)).fetchall()
    return {"reports": [_row(r) for r in rows]}


@router.get("/reports/{report_id}")
def get_report(report_id: str, user: dict = Depends(current_user)) -> Dict[str, Any]:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM diag_reports WHERE id = ?", (report_id,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="no such report")
    if not _is_admin(user) and str(r["user_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="not your report")
    return _row(r)
