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
        # A PDF or an office file is almost always a record; a picture is
        # usually something shown in passing.
        "suggest": "keep" if is_image else "file",
        "raw_url": f"/api/chat/attachments/{row['id']}/raw",
    }


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
    if vis not in ("private", "business", "shared"):
        with get_conn() as conn:
            prof = conn.execute("SELECT default_doc_visibility FROM user_profiles WHERE id = ?", (user_id,)).fetchone()
        vis = (prof["default_doc_visibility"] if prof else None) or ""
        if vis not in ("private", "business", "shared"):
            from .household_settings import get_setting
            vis = get_setting("documents_default_visibility", default="private")
            if vis not in ("private", "business", "shared"):
                vis = "private"
    from . import main as _main        # the write-through lives next to the upload route
    result = _main._push_to_paperless(
        path.read_bytes(), filename=row["filename"], title=title or Path(row["filename"]).stem,
        mime_type=row["mime_type"], tags=[], user_id=user_id, visibility=vis)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error") or result.get("reason") or "Paperless did not take the file"}
    with get_conn() as conn:
        conn.execute("UPDATE chat_attachments SET filed_at = ?, paperless_task_id = ?, visibility = ? WHERE id = ?",
                     (_now(), str(result.get("task_id") or ""), vis, row["id"]))
        conn.commit()
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
        raise HTTPException(status_code=502 if "Paperless" in (result.get("error") or "") else 404,
                            detail=result.get("error"))
    return _public(get(attachment_id, str(user["id"])) or {})


@router.delete("/{attachment_id}", status_code=204)
def delete_route(attachment_id: int, user: Dict[str, Any] = Depends(_current_user())):
    if not delete(attachment_id, str(user["id"])):
        raise HTTPException(status_code=404, detail="no such attachment")
