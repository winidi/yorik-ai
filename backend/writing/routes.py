"""Routes of the "Schreiben" app. Stage 1: the letterhead (Settings →
You → Briefpapier) with its live preview and a sample PDF."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import layouts, letterhead as lh_mod

router = APIRouter(tags=["writing"])


def _user():
    from ..auth_sessions import current_user
    return current_user


def _signed_in(user: Dict[str, Any]) -> str:
    """Bank details and the look of one's letters are changed in person."""
    if user.get("auth") == "api_token":
        raise HTTPException(status_code=403, detail="log in to change this")
    return str(user["id"])


class LetterheadPatch(BaseModel):
    name: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    make_default: bool = False


class PreviewIn(BaseModel):
    kind: str = "letter"
    letterhead_id: Optional[int] = None
    data: Optional[Dict[str, Any]] = None       # unsaved changes, laid over the stored letterhead


@router.get("/api/letterheads")
def list_letterheads(user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    lh_mod.default_for(str(user["id"]))          # nobody starts with an empty sheet
    return {"letterheads": lh_mod.list_for(str(user["id"])),
            "fonts": [{"id": k, "label": v["label"], "css": v["css"]} for k, v in lh_mod.FONTS.items()]}


@router.patch("/api/letterheads/{letterhead_id}")
def patch_letterhead(letterhead_id: int, body: LetterheadPatch, user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    out = lh_mod.update(letterhead_id, _signed_in(user), data=body.data, name=body.name, make_default=body.make_default)
    if not out:
        raise HTTPException(status_code=404, detail="no such letterhead")
    return out


@router.post("/api/letterheads/{letterhead_id}/logo")
async def upload_logo(letterhead_id: int, image: UploadFile = File(...), user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    uid = _signed_in(user)
    try:
        out = lh_mod.save_logo(letterhead_id, uid, await image.read())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not out:
        raise HTTPException(status_code=404, detail="no such letterhead")
    return out


@router.delete("/api/letterheads/{letterhead_id}/logo")
def delete_logo(letterhead_id: int, user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    out = lh_mod.remove_logo(letterhead_id, _signed_in(user))
    if not out:
        raise HTTPException(status_code=404, detail="no such letterhead")
    return out


@router.get("/api/letterheads/{letterhead_id}/logo")
def serve_logo(letterhead_id: int, user: Dict[str, Any] = Depends(_user())):
    if not lh_mod.get(letterhead_id, str(user["id"])) or not lh_mod.logo_path(letterhead_id).exists():
        raise HTTPException(status_code=404, detail="no logo")
    return FileResponse(str(lh_mod.logo_path(letterhead_id)), media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


def _sample(kind: str, body_data: Optional[Dict[str, Any]], letterhead_id: Optional[int], user: Dict[str, Any], *, preview: bool) -> Dict[str, str]:
    if kind not in layouts.KINDS:
        raise HTTPException(status_code=400, detail="kind must be letter, invoice or quote")
    uid = str(user["id"])
    stored = lh_mod.get(letterhead_id, uid) if letterhead_id else lh_mod.default_for(uid)
    if not stored:
        raise HTTPException(status_code=404, detail="no such letterhead")
    data = lh_mod.clean(body_data, stored["data"]) if body_data is not None else stored["data"]
    return layouts.render(kind, data, layouts.SAMPLE_RECIPIENT, layouts.sample_content(kind),
                          logo=lh_mod.logo_data_uri(stored["id"]), preview=preview)


@router.post("/api/letterheads/preview")
def preview_letterhead(body: PreviewIn, user: Dict[str, Any] = Depends(_user())) -> Dict[str, str]:
    """The sample document as one self-contained page for the settings'
    live preview (shown in a sandboxed frame)."""
    return {"html": _sample(body.kind, body.data, body.letterhead_id, user, preview=True)["html"]}


@router.get("/api/letterheads/{letterhead_id}/sample.pdf")
def sample_pdf(letterhead_id: int, kind: str = "letter", user: Dict[str, Any] = Depends(_user())) -> Response:
    from ..compose import pdf as pdf_mod
    page = _sample(kind, None, letterhead_id, user, preview=False)
    blob = pdf_mod.render_html_pdf(page["html"], footer_html=page["footer_html"])
    if not blob:
        raise HTTPException(status_code=502, detail="PDF render failed (Gotenberg unreachable?)")
    return Response(content=blob, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="briefpapier-muster.pdf"'})
