"""written_documents — the rows behind the "Schreiben" app. A draft
changes freely; a final document keeps its number and its PDF and is
not changed any more. Yours only: documents carry other people's
addresses and your bank details.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..database import conn_ctx

KINDS = ("letter", "invoice", "quote")
# restricted (child) accounts write letters, not invoices or quotes
KINDS_RESTRICTED = ("letter",)


class Locked(Exception):
    """The document is final."""


def kinds_for(role: Optional[str]) -> tuple:
    return KINDS_RESTRICTED if (role or "").lower() == "restricted" else KINDS


def _loads(text: Any) -> Dict[str, Any]:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _row(r, *, full: bool = True) -> Dict[str, Any]:
    d = {k: r[k] for k in ("id", "kind", "status", "letterhead_id", "title", "number", "doc_date",
                           "paperless_doc_id", "source_document_id", "created_at", "updated_at", "finalised_at")}
    d["user_id"] = str(r["user_id"])
    d["recipient"] = _loads(r["recipient"])
    d["has_pdf"] = bool(r["pdf_path"])
    if full:
        d["content"] = _loads(r["content"])
    return d


def create(user_id: str, kind: str, *, title: str = "", recipient: Optional[Dict[str, Any]] = None,
           content: Optional[Dict[str, Any]] = None, letterhead_id: Optional[int] = None,
           source_document_id: Optional[int] = None) -> Dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    with conn_ctx() as conn:
        r = conn.execute(
            "INSERT INTO written_documents (user_id, kind, letterhead_id, title, recipient, content, source_document_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (str(user_id), kind, letterhead_id, (title or "").strip()[:200], json.dumps(recipient or {}, ensure_ascii=False),
             json.dumps(content or {}, ensure_ascii=False), source_document_id)).fetchone()
    return get(int(r["id"]), user_id)  # type: ignore[return-value]


def get(doc_id: int, user_id: str) -> Optional[Dict[str, Any]]:
    with conn_ctx() as conn:
        r = conn.execute("SELECT * FROM written_documents WHERE id = ? AND user_id = ?", (int(doc_id), str(user_id))).fetchone()
    return _row(r) if r else None


def list_for(user_id: str, *, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    sql, params = "SELECT * FROM written_documents WHERE user_id = ?", [str(user_id)]
    if status in ("draft", "final"):
        sql += " AND status = ?"; params.append(status)
    with conn_ctx() as conn:
        rows = conn.execute(sql + " ORDER BY updated_at DESC, id DESC LIMIT ?", (*params, max(1, min(int(limit), 500)))).fetchall()
    return [_row(r, full=False) for r in rows]


def update(doc_id: int, user_id: str, *, title: Optional[str] = None, recipient: Optional[Dict[str, Any]] = None,
           content: Optional[Dict[str, Any]] = None, letterhead_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    current = get(doc_id, user_id)
    if not current:
        return None
    if current["status"] == "final":
        raise Locked("a final document does not change")
    sets, params = ["updated_at = ?"], [datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
    if title is not None:
        sets.append("title = ?"); params.append(title.strip()[:200])
    if recipient is not None:
        sets.append("recipient = ?"); params.append(json.dumps(recipient, ensure_ascii=False))
    if content is not None:
        sets.append("content = ?"); params.append(json.dumps(content, ensure_ascii=False))
    if letterhead_id is not None:
        sets.append("letterhead_id = ?"); params.append(int(letterhead_id))
    with conn_ctx() as conn:
        conn.execute(f"UPDATE written_documents SET {', '.join(sets)} WHERE id = ?", (*params, int(doc_id)))
    return get(doc_id, user_id)


def delete(doc_id: int, user_id: str) -> bool:
    """Drafts only: an issued invoice must stay on record."""
    current = get(doc_id, user_id)
    if not current:
        return False
    if current["status"] == "final":
        raise Locked("a final document is kept")
    with conn_ctx() as conn:
        conn.execute("DELETE FROM written_documents WHERE id = ?", (int(doc_id),))
    return True


def finalise(doc_id: int, user_id: str, *, pdf_path: str, number: Optional[str] = None,
             doc_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The document has left the house (sent, filed, issued): from now
    on it is what its PDF says."""
    current = get(doc_id, user_id)
    if not current:
        return None
    if current["status"] == "final":
        return current
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn_ctx() as conn:
        conn.execute("UPDATE written_documents SET status = 'final', pdf_path = ?, number = COALESCE(?, number), "
                     "doc_date = ?, finalised_at = ?, updated_at = ? WHERE id = ?",
                     (pdf_path, number, doc_date or now[:10], now, now, int(doc_id)))
    return get(doc_id, user_id)


def pdf_path_of(doc_id: int, user_id: str) -> Optional[str]:
    with conn_ctx() as conn:
        r = conn.execute("SELECT pdf_path FROM written_documents WHERE id = ? AND user_id = ?", (int(doc_id), str(user_id))).fetchone()
    return r["pdf_path"] if r and r["pdf_path"] else None


def set_paperless(doc_id: int, *, paperless_doc_id: Optional[int] = None) -> None:
    with conn_ctx() as conn:
        conn.execute("UPDATE written_documents SET paperless_doc_id = ? WHERE id = ?", (paperless_doc_id, int(doc_id)))


def duplicate(doc_id: int, user_id: str) -> Optional[Dict[str, Any]]:
    """A fresh draft with the same recipient and content."""
    src = get(doc_id, user_id)
    if not src:
        return None
    content = {k: v for k, v in src["content"].items() if k not in ("number", "date")}
    return create(user_id, src["kind"], title=src["title"], recipient=src["recipient"], content=content,
                  letterhead_id=src["letterhead_id"])

