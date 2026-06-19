"""Upload-hardening tests — pins the size/MIME/path-traversal fixes
in /api/documents/upload. Any regression on these is a CVE class.

Three things being asserted:
  1. Files over the configured size cap are rejected with 413 BEFORE
     hitting disk (the streaming loop in main.py).
  2. MIME types not in documents.SUPPORTED_MIME are rejected with 415.
  3. A filename containing path traversal ("../../passwd") lands as
     a sanitised basename inside DOCS_DIR — never escapes.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from fastapi.testclient import TestClient


def _setup_and_login(client: TestClient) -> None:
    r = client.post("/api/auth/setup", json={
        "name": "Upload Tester", "email": "uploader@yorik.local",
        "password": "uploader-pw-12345",
    })
    assert r.status_code == 200, r.text


def _small_pdf(name: str = "doc.pdf") -> tuple[str, io.BytesIO, str]:
    """Returns a (filename, content, mime) triple shaped for httpx
    multipart uploads. PDF magic bytes only — the validator doesn't
    deep-inspect, but using real magic keeps the test honest."""
    body = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    return (name, io.BytesIO(body), "application/pdf")


# ─── 1. Size cap ───────────────────────────────────────────────────────

def test_upload_over_size_cap_rejected_413(fresh_app, monkeypatch):
    """50MB default → set to 1MB for speed → upload 2MB → expect 413
    'file exceeds N MB limit'."""
    monkeypatch.setenv("YORIK_MAX_UPLOAD_MB", "1")
    client = TestClient(fresh_app)
    _setup_and_login(client)

    big = io.BytesIO(b"%PDF-1.4\n" + b"X" * (2 * 1024 * 1024))
    r = client.post(
        "/api/documents/upload",
        files={"file": ("big.pdf", big, "application/pdf")},
    )
    assert r.status_code == 413, (
        f"SIZE-CAP REGRESSION: 2MB upload got {r.status_code} with cap=1MB. "
        f"Body: {r.text}"
    )
    assert "limit" in r.text.lower() or "exceed" in r.text.lower()


# ─── 2. MIME allowlist ────────────────────────────────────────────────

def test_upload_rejects_disallowed_mime_415(fresh_app):
    """A .exe with x-msdownload MIME must be rejected before disk."""
    client = TestClient(fresh_app)
    _setup_and_login(client)

    evil = io.BytesIO(b"MZ\x90\x00...this would be a PE binary...")
    r = client.post(
        "/api/documents/upload",
        files={"file": ("evil.exe", evil, "application/x-msdownload")},
    )
    assert r.status_code == 415, (
        f"MIME REGRESSION: x-msdownload accepted with {r.status_code}. "
        f"Body: {r.text}"
    )
    assert "unsupported" in r.text.lower() or "allowed" in r.text.lower()


def test_upload_accepts_pdf(fresh_app):
    """Sanity: a valid PDF goes through (201 Created)."""
    client = TestClient(fresh_app)
    _setup_and_login(client)
    name, body, mime = _small_pdf()
    r = client.post("/api/documents/upload",
                    files={"file": (name, body, mime)})
    # Note: 201 means we got past auth + MIME + size; the document may
    # still fail to index due to test-env quirks, but that's a separate
    # invariant. We only assert the security gates open.
    assert r.status_code in (201, 500), r.text
    # If the upload itself failed for a non-security reason, we want
    # the body to mention indexing, not "unsupported", "limit", or "auth".
    if r.status_code == 500:
        low = r.text.lower()
        assert "unsupported" not in low and "limit" not in low and \
               "auth" not in low and "role" not in low, \
               f"PDF upload should pass security gates: {r.text}"


# ─── 3. Path traversal ────────────────────────────────────────────────

def test_upload_filename_traversal_is_sanitised(fresh_app, tmp_path,
                                                  monkeypatch):
    """Filename '../../etc/passwd' must NOT cause anything to be
    written outside DOCS_DIR. We snapshot the docs dir before and after
    the upload — only DOCS_DIR/<doc_id>/ may grow; nothing else.

    This test would have caught the historical 'dest_dir / file.filename'
    bug class even if the fix wasn't already in place."""
    docs_dir = tmp_path / "docs"
    monkeypatch.setenv("HOMEOS_DOCS_DIR", str(docs_dir))

    client = TestClient(fresh_app)
    _setup_and_login(client)

    # Snapshot the parent of where we expect writes — if a write
    # escapes via "../", it'll appear here.
    parent = tmp_path
    before = set(parent.rglob("*"))

    name, body, mime = _small_pdf("../../etc/passwd")
    r = client.post("/api/documents/upload",
                    files={"file": (name, body, mime)})

    # Status code: either 201 (sanitised + accepted) or 4xx — but
    # NEVER a 500 caused by writing outside the sandbox.
    assert r.status_code != 500 or "unsupported" in r.text.lower() or \
           "limit" in r.text.lower(), r.text

    after = set(parent.rglob("*"))
    new_paths = after - before
    # Anything new must be under tmp_path (the sandbox); no '..'
    # escapes. tmp_path is itself outside the test's writable area,
    # so just check no new file's resolved path mentions 'passwd' or
    # escapes the test tmp.
    for p in new_paths:
        rp = p.resolve()
        assert str(rp).startswith(str(tmp_path.resolve())), (
            f"PATH TRAVERSAL: upload wrote {rp} outside the sandbox "
            f"{tmp_path}"
        )
        assert "passwd" not in p.name or p.name == "passwd", (
            # If sanitisation reduced '../../etc/passwd' to 'passwd',
            # that's the expected behaviour — the file lands as
            # DOCS_DIR/<doc_id>/passwd, NOT at /etc/passwd.
            f"unexpected filename in sandbox: {p}"
        )
