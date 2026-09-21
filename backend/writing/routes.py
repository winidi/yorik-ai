"""Routes of the "Schreiben" app: the letterhead (Settings → You) with
its live preview, and the documents under /api/writing — drafts that
become final the moment they leave the house (sent or filed).
/api/documents is the Paperless side and not ours."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import layouts, letterhead as lh_mod, recipient as rcp_mod, store

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


# ── documents ───────────────────────────────────────────────────────────

# Invoices and quotes join in stage 3 (arithmetic is there, numbering,
# mandatory fields and the e-invoice are not): until then, letters.
ENABLED_KINDS = ("letter",)
PDF_DIR_ENV = "YORIK_WRITTEN_DIR"


def _pdf_dir():
    import os
    from pathlib import Path
    d = Path(os.getenv(PDF_DIR_ENV, "data/written"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _kinds(user: Dict[str, Any]) -> list:
    return [k for k in store.kinds_for(user.get("role")) if k in ENABLED_KINDS]


class DocIn(BaseModel):
    kind: str = "letter"
    title: Optional[str] = None
    recipient: Optional[Dict[str, Any]] = None
    recipient_query: Optional[str] = None        # a name to look up in the contacts
    contact_id: Optional[int] = None
    content: Optional[Dict[str, Any]] = None


class DocPatch(BaseModel):
    title: Optional[str] = None
    recipient: Optional[Dict[str, Any]] = None
    contact_id: Optional[int] = None
    content: Optional[Dict[str, Any]] = None
    letterhead_id: Optional[int] = None


def _letterhead_of(doc: Dict[str, Any], uid: str) -> Dict[str, Any]:
    return (lh_mod.get(doc["letterhead_id"], uid) if doc.get("letterhead_id") else None) or lh_mod.default_for(uid)


def _recipient(body_recipient, contact_id, query, user: Dict[str, Any], country: str) -> Optional[Dict[str, Any]]:
    if contact_id:
        from .. import contacts as _contacts
        c = _contacts.get(int(contact_id), role=user.get("role"), user_id=user["id"])
        if not c:
            raise HTTPException(status_code=404, detail="no such contact")
        return rcp_mod.from_contact(c, sender_country=country)
    if body_recipient is not None:
        return rcp_mod.clean(body_recipient)
    if query:
        return rcp_mod.resolve(query, role=user.get("role"), user_id=user["id"], sender_country=country)
    return None


def _clean_content(kind: str, raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    if kind == "letter":
        out = {"subject": str(raw.get("subject") or "").strip()[:200],
               "text_html": layouts.sanitise(raw.get("text_html")) or layouts.text_to_html(raw.get("text")),
               "add_closing": bool(raw.get("add_closing", False))}
        for k in ("date", "your_ref", "our_ref"):
            if raw.get(k):
                out[k] = str(raw[k]).strip()[:40]
        return out
    return raw


def _render(doc: Dict[str, Any], uid: str, *, preview: bool) -> Dict[str, str]:
    lh = _letterhead_of(doc, uid)
    return layouts.render(doc["kind"], lh["data"], doc["recipient"], doc["content"],
                          logo=lh_mod.logo_data_uri(lh["id"]), preview=preview)


def make_pdf(doc: Dict[str, Any], uid: str) -> bytes:
    from ..compose import pdf as pdf_mod
    page = _render(doc, uid, preview=False)
    blob = pdf_mod.render_html_pdf(page["html"], footer_html=page["footer_html"])
    if not blob:
        raise HTTPException(status_code=502, detail="PDF render failed (Gotenberg unreachable?)")
    return blob


def _finalise(doc: Dict[str, Any], uid: str, blob: Optional[bytes] = None) -> Dict[str, Any]:
    """Freeze the document as its PDF. Idempotent."""
    if doc["status"] == "final":
        return doc
    blob = blob or make_pdf(doc, uid)
    path = _pdf_dir() / f"{int(doc['id'])}.pdf"
    path.write_bytes(blob)
    return store.finalise(doc["id"], uid, pdf_path=str(path), doc_date=doc["content"].get("date")) or doc


def _final_pdf(doc: Dict[str, Any], uid: str) -> bytes:
    from pathlib import Path
    p = store.pdf_path_of(doc["id"], uid)
    if doc["status"] == "final" and p and Path(p).exists():
        return Path(p).read_bytes()
    return make_pdf(doc, uid)


def _filename(doc: Dict[str, Any]) -> str:
    import re
    base = re.sub(r"[^\w .-]+", "", doc["title"] or doc["content"].get("subject") or "Brief", flags=re.UNICODE).strip()[:80] or "Brief"
    return f"{base}.pdf"


def _disposition(name: str) -> str:
    """A header is latin-1 at best: "Kündigung.pdf" goes in twice, plain
    for old clients and UTF-8 (RFC 5987) for everybody else."""
    from urllib.parse import quote
    plain = name.encode("ascii", "ignore").decode("ascii").replace('"', "") or "document.pdf"
    return f"inline; filename=\"{plain}\"; filename*=UTF-8''{quote(name)}"


def _doc_or_404(doc_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
    doc = store.get(doc_id, str(user["id"]))
    if not doc:
        raise HTTPException(status_code=404, detail="no such document")
    return doc


@router.get("/api/writing")
def list_docs(status: Optional[str] = None, user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    return {"documents": store.list_for(str(user["id"]), status=status), "kinds": _kinds(user)}


@router.post("/api/writing", status_code=201)
def create_doc(body: DocIn, user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    if body.kind not in _kinds(user):
        raise HTTPException(status_code=403 if body.kind in store.KINDS else 400, detail=f"cannot write a {body.kind}")
    uid = str(user["id"])
    lh = lh_mod.default_for(uid)
    rcp = _recipient(body.recipient, body.contact_id, body.recipient_query, user, lh["data"]["country"]) or rcp_mod.clean({})
    content = _clean_content(body.kind, body.content)
    return store.create(uid, body.kind, title=body.title or content.get("subject") or "", recipient=rcp, content=content, letterhead_id=lh["id"])


@router.get("/api/writing/{doc_id}")
def get_doc(doc_id: int, user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    return _doc_or_404(doc_id, user)


@router.patch("/api/writing/{doc_id}")
def patch_doc(doc_id: int, body: DocPatch, user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    doc = _doc_or_404(doc_id, user)
    uid = str(user["id"])
    if body.letterhead_id is not None and not lh_mod.get(body.letterhead_id, uid):
        raise HTTPException(status_code=404, detail="no such letterhead")
    rcp = _recipient(body.recipient, body.contact_id, None, user, _letterhead_of(doc, uid)["data"]["country"])
    content = _clean_content(doc["kind"], body.content) if body.content is not None else None
    # a letter is called what its subject says, unless it was given a name of its own
    title = body.title
    if title is None and content is not None and doc["kind"] == "letter" and doc["title"] == (doc["content"].get("subject") or ""):
        title = content.get("subject") or ""
    try:
        out = store.update(doc_id, uid, title=title, recipient=rcp, letterhead_id=body.letterhead_id, content=content)
    except store.Locked:
        raise HTTPException(status_code=409, detail="this document is final; duplicate it to change it")
    return out  # type: ignore[return-value]


@router.delete("/api/writing/{doc_id}", status_code=204, response_class=Response)
def delete_doc(doc_id: int, user: Dict[str, Any] = Depends(_user())) -> Response:
    _doc_or_404(doc_id, user)
    try:
        store.delete(doc_id, str(user["id"]))
    except store.Locked:
        raise HTTPException(status_code=409, detail="a final document is kept")
    return Response(status_code=204)


@router.post("/api/writing/{doc_id}/duplicate", status_code=201)
def duplicate_doc(doc_id: int, user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    _doc_or_404(doc_id, user)
    return store.duplicate(doc_id, str(user["id"]))  # type: ignore[return-value]


@router.get("/api/writing/{doc_id}/preview")
def preview_doc(doc_id: int, user: Dict[str, Any] = Depends(_user())) -> Dict[str, str]:
    return {"html": _render(_doc_or_404(doc_id, user), str(user["id"]), preview=True)["html"]}


@router.get("/api/writing/{doc_id}/pdf")
def pdf_doc(doc_id: int, user: Dict[str, Any] = Depends(_user())) -> Response:
    doc = _doc_or_404(doc_id, user)
    return Response(content=_final_pdf(doc, str(user["id"])), media_type="application/pdf",
                    headers={"Content-Disposition": _disposition(_filename(doc))})


class FileIn(BaseModel):
    visibility: Optional[str] = None             # private | parents | shared; default: the person's usual


@router.post("/api/writing/{doc_id}/paperless")
def file_doc(doc_id: int, body: FileIn, user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    """Into Paperless through the person's own token, like an upload in
    the Documents app. Filing makes the document final."""
    doc = _doc_or_404(doc_id, user)
    uid = _signed_in(user)
    blob = _final_pdf(doc, uid)
    from .. import main as _main, chat_attachments as _att
    vis = (body.visibility or "").strip().lower()
    if vis not in ("private", "parents", "business", "shared"):
        vis = _att._default_visibility(uid)
    name = _filename(doc)
    result = _main._push_to_paperless(blob, filename=name, title=name[:-4], mime_type="application/pdf", tags=[], user_id=uid, visibility=vis)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or result.get("reason") or "Paperless did not take the file")
    doc = _finalise(doc, uid, blob)
    if result.get("task_id"):
        from .. import paperless_visibility as _pv
        _pv.apply_after_consume(str(result["task_id"]), vis, on_document=lambda pid, d=doc["id"]: store.set_paperless(d, paperless_doc_id=int(pid)))
    return {"ok": True, "visibility": vis, "document": doc}


class SendIn(BaseModel):
    account_id: int
    to: str
    subject: str
    message: str = ""


@router.post("/api/writing/{doc_id}/send")
def send_doc(doc_id: int, body: SendIn, user: Dict[str, Any] = Depends(_user())) -> Dict[str, Any]:
    """The PDF as an attachment, from one of the person's own mail
    accounts. Sending makes the document final."""
    doc = _doc_or_404(doc_id, user)
    uid = _signed_in(user)
    from ..database import conn_ctx
    with conn_ctx() as conn:
        own = conn.execute("SELECT 1 FROM email_accounts WHERE id = ? AND owner_user_id = ?", (int(body.account_id), uid)).fetchone()
    if not own:
        raise HTTPException(status_code=404, detail="no such mail account")
    to = [t.strip() for t in body.to.replace(";", ",").split(",") if t.strip()]
    if not to or not all("@" in t for t in to):
        raise HTTPException(status_code=400, detail="a recipient address is needed")
    blob = _final_pdf(doc, uid)
    from .. import email_sender as _sender
    result = _sender.send(int(body.account_id), to, body.subject.strip() or (doc["title"] or "Brief"),
                          body.message.strip() or "Anbei das Schreiben als PDF.",
                          attachments=[{"filename": _filename(doc), "mimetype": "application/pdf", "content": blob}])
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or "the mail could not be sent")
    return {"ok": True, "document": _finalise(doc, uid, blob)}


class RewriteIn(BaseModel):
    text: str
    instruction: str


@router.post("/api/writing/rewrite")
def rewrite(body: RewriteIn, user: Dict[str, Any] = Depends(_user())) -> Dict[str, str]:
    """"Make it friendlier", "shorter", "in English": the marked passage
    (or the whole letter) rewritten by the local model. Text in, text
    out — the look is not the model's business."""
    import os
    text, wish = body.text.strip()[:12000], body.instruction.strip()[:300]
    if not text or not wish:
        raise HTTPException(status_code=400, detail="text and instruction are needed")
    from ..agent.llm import LlmClient
    client = LlmClient(model=os.getenv("HOMEOS_MODEL", "qwen3.5-9b"), base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"))
    prompt = ("Du überarbeitest einen Abschnitt aus einem Brief. Gib NUR den überarbeiteten Text zurück, ohne Einleitung, ohne Anführungszeichen, "
              "ohne Erklärungen, in derselben Sprache wie der Text (außer der Wunsch verlangt eine andere). Absätze durch Leerzeilen trennen.\n\n"
              f"Wunsch: {wish}\n\nText:\n{text}")
    try:
        resp = client.chat(messages=[{"role": "user", "content": prompt}], max_tokens=1500, temperature=0.4)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"the model did not answer ({type(exc).__name__})")
    out = str(resp.get("content") or "").strip().strip('"„“').strip()
    if not out:
        raise HTTPException(status_code=502, detail="the model returned nothing; try another wording")
    return {"text": out}

