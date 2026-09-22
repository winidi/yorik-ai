"""Files shown to Yorik in a chat.

One place for documents: Paperless. A file dropped into the chat is an
attachment of that conversation — the assistant reads it, the person
decides whether it is a record worth filing. Filed: it goes to
Paperless through the person's own token and lives there. Not filed:
it stays a card in the conversation and is deleted with the
conversation or after RETENTION_DAYS. There is no list, no folder, no
second library.

Only the owner sees an attachment (no admin exception).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from .database import get_conn

log = logging.getLogger("yorik.chat_attachments")

RETENTION_DAYS = int(os.getenv("YORIK_CHAT_ATTACHMENT_DAYS", "30"))
MAX_BYTES = int(os.getenv("YORIK_MAX_UPLOAD_MB", "50")) * 1024 * 1024
TEXT_CAP = 40_000
ROOT = Path(os.getenv("YORIK_DATA_DIR", Path(__file__).resolve().parent.parent / "data")) / "chat_attachments"

_MIME_BY_EXT = {
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    is_image = (row["mime_type"] or "").startswith("image/")
    return {
        "id": row["id"], "filename": row["filename"], "mime_type": row["mime_type"], "bytes": row["bytes"],
        "is_image": is_image, "has_text": bool((row.get("text") or "").strip()),
        "conversation_id": row["conversation_id"], "created_at": row["created_at"], "expires_at": row["expires_at"],
        "filed": bool(row["filed_at"]), "filed_at": row["filed_at"], "visibility": row["visibility"],
        "paperless_doc_id": row.get("paperless_doc_id"),
        "paperless_error": row.get("paperless_error"),
        # A PDF or an office file is almost always a record; a picture is
        # usually something shown in passing.
        "suggest": "keep" if is_image else "file",
        "default_visibility": _default_visibility(str(row["owner_user_id"])),
        "raw_url": f"/api/chat/attachments/{row['id']}/raw",
    }


def _default_visibility(user_id: str) -> str:
    """The person's own default for new documents, else the household's, else private."""
    with get_conn() as conn:
        prof = conn.execute("SELECT default_doc_visibility FROM user_profiles WHERE id = ?", (user_id,)).fetchone()
    vis = (prof["default_doc_visibility"] if prof else None) or ""
    if vis not in ("private", "parents", "business", "shared"):
        from .household_settings import get_setting
        vis = get_setting("documents_default_visibility", default="private")
    return vis if vis in ("private", "parents", "business", "shared") else "private"


def get(attachment_id: int, user_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chat_attachments WHERE id = ? AND owner_user_id = ?",
                           (int(attachment_id), user_id)).fetchone()
    return dict(row) if row else None


def store(*, user_id: str, filename: str, mime_type: str, data: bytes,
          conversation_id: Optional[str] = None) -> Dict[str, Any]:
    safe = Path(filename or "upload.bin").name
    mime = (mime_type or "").lower() or _MIME_BY_EXT.get(Path(safe).suffix.lower(), "application/octet-stream")
    if mime == "application/octet-stream":
        mime = _MIME_BY_EXT.get(Path(safe).suffix.lower(), mime)
    now = datetime.now()
    with get_conn() as conn:
        att_id = conn.execute(
            "INSERT INTO chat_attachments (owner_user_id, conversation_id, filename, mime_type, bytes, path, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, '', ?, ?) RETURNING id",
            (user_id, conversation_id, safe, mime, len(data), now.isoformat(timespec="seconds"),
             (now + timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")),
        ).fetchone()["id"]
        folder = ROOT / str(att_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe
        path.write_bytes(data)
        text = ""
        if not mime.startswith("image/"):
            try:
                from . import documents as _docs
                text = (_docs.extract_text(path, mime if mime in _docs.SUPPORTED_MIME else None) or "").replace("\x00", "")
            except Exception as exc:  # noqa: BLE001
                log.info("chat attachment %s: no text extracted (%s)", att_id, exc)
        conn.execute("UPDATE chat_attachments SET path = ?, text = ? WHERE id = ?", (str(path), text, att_id))
        conn.commit()
    return get(att_id, user_id) or {}


def bind_conversation(attachment_id: int, user_id: str, conversation_id: str) -> None:
    """An upload into a brand-new chat has no conversation yet; the first
    skill call that reads it ties it to the conversation."""
    with get_conn() as conn:
        conn.execute("UPDATE chat_attachments SET conversation_id = ? WHERE id = ? AND owner_user_id = ? "
                     "AND conversation_id IS NULL", (conversation_id, int(attachment_id), user_id))
        conn.commit()


def _remove_files(row: Dict[str, Any]) -> None:
    try:
        shutil.rmtree(ROOT / str(row["id"]), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def delete(attachment_id: int, user_id: str) -> bool:
    row = get(attachment_id, user_id)
    if not row:
        return False
    _remove_files(row)
    with get_conn() as conn:
        conn.execute("DELETE FROM chat_attachments WHERE id = ?", (row["id"],))
        conn.commit()
    return True


def delete_for_conversation(conversation_id: str) -> int:
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM chat_attachments WHERE conversation_id = ?", (conversation_id,)).fetchall()]
        for r in rows:
            _remove_files(r)
        conn.execute("DELETE FROM chat_attachments WHERE conversation_id = ?", (conversation_id,))
        conn.commit()
    return len(rows)


def purge_expired() -> int:
    """Past the retention period: the file goes. A filed attachment
    lives on in Paperless; only the chat's copy is removed."""
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM chat_attachments WHERE expires_at < ?", (_now(),)).fetchall()]
        for r in rows:
            _remove_files(r)
        conn.execute("DELETE FROM chat_attachments WHERE expires_at < ?", (_now(),))
        conn.commit()
    return len(rows)


def file_in_paperless(attachment_id: int, user_id: str, visibility: Optional[str] = None,
                      title: Optional[str] = None) -> Dict[str, Any]:
    """Send the attachment to Paperless through the person's own token
    (same path as an upload in the Documents app). Idempotent."""
    row = get(attachment_id, user_id)
    if not row:
        return {"ok": False, "error": "no such attachment"}
    if row["filed_at"]:
        return {"ok": True, "already_filed": True, "filed_at": row["filed_at"]}
    path = Path(row["path"])
    if not path.exists():
        return {"ok": False, "error": "the file is gone (retention) — upload it again"}
    vis = (visibility or "").strip().lower()
    if vis not in ("private", "parents", "business", "shared"):
        vis = _default_visibility(user_id)
    from . import main as _main        # the write-through lives next to the upload route
    result = _main._push_to_paperless(
        path.read_bytes(), filename=row["filename"], title=title or Path(row["filename"]).stem,
        mime_type=row["mime_type"], tags=[], user_id=user_id, visibility=vis)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or result.get("reason") or "Paperless did not take the file"}
    task_id = str(result.get("task_id") or "")
    att_id = int(row["id"])
    from . import paperless_visibility as _pv

    def _remember(doc_id: int) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE chat_attachments SET paperless_doc_id = ?, paperless_error = NULL WHERE id = ?",
                         (int(doc_id), att_id))
            conn.commit()

    def _failed(reason: str) -> None:
        # A duplicate means the document is in Paperless already — that
        # is "filed", pointing at the existing one. Anything else is not.
        existing = _pv.duplicate_of(reason)
        with get_conn() as conn:
            if existing:
                conn.execute("UPDATE chat_attachments SET paperless_doc_id = ?, paperless_error = NULL WHERE id = ?",
                             (existing, att_id))
            else:
                conn.execute("UPDATE chat_attachments SET filed_at = NULL, paperless_task_id = NULL, "
                             "paperless_error = ? WHERE id = ?", ((reason or "Paperless refused the file")[:300], att_id))
            conn.commit()

    # Paperless refuses a duplicate or a broken file within seconds of
    # the upload; a good file takes longer (OCR). Wait a moment for the
    # verdict so the card does not say "filed" for a refused file (the
    # 2026-09-21 case); after that the background waiter reports.
    import time
    state = None
    for _ in range(8 if task_id and _pv._settings().get("api_key") else 0):
        state = _pv.task_state(task_id)
        if state and state["status"] in ("SUCCESS", "FAILURE"):
            break
        time.sleep(1)
    if state and state["status"] == "FAILURE":
        existing = _pv.duplicate_of(state["result"])
        if not existing:
            with get_conn() as conn:
                conn.execute("UPDATE chat_attachments SET paperless_error = ? WHERE id = ?",
                             ((state["result"] or "Paperless refused the file")[:300], att_id))
                conn.commit()
            return {"ok": False, "refused": True, "error": state["result"] or "Paperless refused the file"}
        with get_conn() as conn:
            conn.execute("UPDATE chat_attachments SET filed_at = ?, paperless_task_id = ?, visibility = ?, "
                         "paperless_doc_id = ?, paperless_error = NULL WHERE id = ?",
                         (_now(), task_id, vis, existing, att_id))
            conn.commit()
        return {"ok": True, "filed_at": _now(), "visibility": vis, "already_in_paperless": True, "paperless_doc_id": existing}

    with get_conn() as conn:
        conn.execute("UPDATE chat_attachments SET filed_at = ?, paperless_task_id = ?, visibility = ?, paperless_error = NULL "
                     "WHERE id = ?", (_now(), task_id, vis, att_id))
        conn.commit()
    if state and state["status"] == "SUCCESS" and state.get("related_document"):
        _remember(state["related_document"])
    elif task_id:
        # _push_to_paperless applies a non-private visibility after the
        # consume on its own; this waiter is for the document id and the
        # failure verdict (a second permissions call is idempotent).
        _pv.apply_after_consume(task_id, vis, on_document=_remember, on_failure=_failed)
    return {"ok": True, "filed_at": _now(), "visibility": vis}


async def describe_image(row: Dict[str, Any], question: Optional[str] = None) -> str:
    """Let the multimodal model read a picture (photo of a letter, a
    screenshot). Same endpoint as every other in-skill LLM call."""
    from .agent.llm import LlmClient
    data = Path(row["path"]).read_bytes()
    prompt = question or ("Beschreibe knapp, was auf dem Bild zu sehen ist. Steht Text darauf, gib ihn vollständig "
                          "und wörtlich wieder (Absender, Datum, Beträge, Nummern).")
    client = LlmClient(model=os.getenv("HOMEOS_MODEL", "qwen3.5-9b"),
                       base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"), request_timeout=120)
    content = [{"type": "text", "text": prompt},
               {"type": "image_url", "image_url": {"url": f"data:{row['mime_type']};base64,{base64.b64encode(data).decode()}"}}]
    result = await asyncio.to_thread(client.chat, [{"role": "user", "content": content}], max_tokens=900, temperature=0.1)
    return (result.get("content") or "").strip()


SCAN_MAX_PAGES = 8      # a letter or an invoice; more pages → file it, Paperless runs OCR
SCAN_DPI = 150


def _render_pdf_pages(pdf_path: str, tmpdir: str, last: int) -> list[str]:
    import glob
    import subprocess
    prefix = os.path.join(tmpdir, "page")
    try:
        subprocess.run(["pdftoppm", "-png", "-r", str(SCAN_DPI), "-f", "1", "-l", str(last), pdf_path, prefix],
                       capture_output=True, timeout=90)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("chat attachment: pdftoppm failed for %s: %s", pdf_path, exc)
        return []
    return sorted(glob.glob(f"{prefix}-*.png"))


async def read_scanned_pdf(row: Dict[str, Any]) -> tuple[str, int, int]:
    """A PDF without a text layer (a scan): render the first pages and
    let the vision model transcribe them. Returns (text, pages_read,
    pages_total). The transcript is kept on the row, so it is read once."""
    import tempfile
    from .agent.llm import LlmClient
    try:
        from pypdf import PdfReader
        total = len(PdfReader(row["path"]).pages)
    except Exception:  # noqa: BLE001
        total = SCAN_MAX_PAGES
    last = min(total, SCAN_MAX_PAGES)
    client = LlmClient(model=os.getenv("HOMEOS_MODEL", "qwen3.5-9b"),
                       base_url=os.getenv("HOMEOS_LLM_BASE_URL", "http://127.0.0.1:8080/v1"), request_timeout=240)
    parts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="yca-") as tmpdir:
        pages = await asyncio.to_thread(_render_pdf_pages, row["path"], tmpdir, last)
        for n, png in enumerate(pages, start=1):
            b64 = base64.b64encode(Path(png).read_bytes()).decode()
            content = [{"type": "text", "text": "Gib den gesamten Text dieser Seite wörtlich und vollständig wieder, in "
                                                "Lesereihenfolge, Tabellen zeilenweise. Keine Kommentare, keine Zusammenfassung."},
                       {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]
            result = await asyncio.to_thread(client.chat, [{"role": "user", "content": content}],
                                             max_tokens=1800, temperature=0.0)
            parts.append(f"[Seite {n}]\n{(result.get('content') or '').strip()}")
    text = "\n\n".join(parts).strip()
    if text:
        with get_conn() as conn:
            conn.execute("UPDATE chat_attachments SET text = ? WHERE id = ?", (text, row["id"]))
            conn.commit()
    return text, len(parts), total


# ─── scheduler ───────────────────────────────────────────────────────

_task = None


def start_scheduler(loop: asyncio.AbstractEventLoop) -> None:
    from . import workers
    global _task
    workers.register("chat-attachments", kind="retention", expected_interval_s=6 * 3600)

    async def _loop():
        while True:
            try:
                n = await asyncio.get_running_loop().run_in_executor(None, purge_expired)
                workers.heartbeat("chat-attachments", "ok", f"{n} expired attachments removed")
            except Exception as exc:  # noqa: BLE001
                log.warning("chat attachments: purge failed: %s", exc)
            await asyncio.sleep(6 * 3600)

    _task = loop.create_task(_loop(), name="chat-attachments-retention")


# ─── routes ──────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/chat/attachments", tags=["chat"])


def _current_user():
    from .auth_sessions import current_user
    return current_user


@router.post("", status_code=201)
async def upload(file: UploadFile = File(...), conversation_id: Optional[str] = Query(None),
                 user: Dict[str, Any] = Depends(_current_user())) -> Dict[str, Any]:
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"file exceeds {MAX_BYTES // (1024 * 1024)} MB")
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    row = await asyncio.to_thread(store, user_id=str(user["id"]), filename=file.filename or "upload.bin",
                                  mime_type=file.content_type or "", data=data, conversation_id=conversation_id)
    return _public(row)


@router.get("/{attachment_id}")
def meta(attachment_id: int, user: Dict[str, Any] = Depends(_current_user())) -> Dict[str, Any]:
    row = get(attachment_id, str(user["id"]))
    if not row:
        raise HTTPException(status_code=404, detail="no such attachment")
    return _public(row)


@router.get("/{attachment_id}/raw")
def raw(attachment_id: int, user: Dict[str, Any] = Depends(_current_user())):
    row = get(attachment_id, str(user["id"]))
    if not row or not Path(row["path"]).exists():
        raise HTTPException(status_code=404, detail="no such attachment")
    return FileResponse(row["path"], media_type=row["mime_type"], filename=row["filename"],
                        content_disposition_type="inline")


@router.post("/{attachment_id}/file")
def file_route(attachment_id: int, visibility: Optional[str] = Query(None),
               user: Dict[str, Any] = Depends(_current_user())) -> Dict[str, Any]:
    result = file_in_paperless(attachment_id, str(user["id"]), visibility)
    if not result.get("ok"):
        code = 422 if result.get("refused") else 502 if "Paperless" in (result.get("error") or "") else 404
        raise HTTPException(status_code=code, detail=result.get("error"))
    return _public(get(attachment_id, str(user["id"])) or {})


@router.delete("/{attachment_id}", status_code=204)
def delete_route(attachment_id: int, user: Dict[str, Any] = Depends(_current_user())):
    if not delete(attachment_id, str(user["id"])):
        raise HTTPException(status_code=404, detail="no such attachment")
