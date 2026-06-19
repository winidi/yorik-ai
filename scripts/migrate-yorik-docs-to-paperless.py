#!/usr/bin/env python3
"""One-shot migration: push every doc that lives only in Yorik's local
store into Paperless.

Background: until commit "Paperless write-through on uploads", documents
uploaded via the React Documents app went to Yorik's local sqlite-vec
store (data/documents/) but were NEVER mirrored into Paperless. Users
who clicked "Open in Paperless" saw an empty inbox.

This script reads the local docs table, posts each file to Paperless's
/api/documents/post_document/ via the admin token, and prints a short
summary. Idempotent at the title level — re-running won't duplicate
because Paperless's consume pipeline dedupes by checksum.

Usage:
  source venv/bin/activate
  python scripts/migrate-yorik-docs-to-paperless.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

# Ensure we can import the backend package when invoked from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import documents as docs_mod  # noqa: E402
from backend.connectors.paperless import _settings as paperless_settings  # noqa: E402


def main() -> int:
    s = paperless_settings()
    api_key = s.get("api_key")
    if not api_key:
        print("✗ Paperless admin token not configured. Run start.sh first.", file=sys.stderr)
        return 1
    base_url = (s.get("base_url") or "http://localhost:8010").rstrip("/")
    headers = {"Authorization": f"Token {api_key}"}

    docs = docs_mod.list_documents(role="admin")
    if not docs:
        print("· no local docs to migrate.")
        return 0

    pushed = 0
    skipped = 0
    failed = 0
    print(f"Found {len(docs)} local doc(s). Pushing to Paperless at {base_url}…\n")
    for d in docs:
        doc_id = d["id"]
        full = docs_mod.get_document(doc_id) or {}
        path = full.get("path")
        if not path or not Path(path).exists():
            print(f"  ✗ #{doc_id} {d['title']!r}: file missing on disk ({path}); skipping")
            skipped += 1
            continue
        with open(path, "rb") as fh:
            body = fh.read()
        title = d.get("title") or Path(path).name
        try:
            r = requests.post(
                f"{base_url}/api/documents/post_document/",
                headers=headers,
                files={"document": (Path(path).name, body, d.get("mime_type") or "application/octet-stream")},
                data={"title": title},
                timeout=20,
            )
            if r.ok:
                task_id = r.text.strip().strip('"')
                print(f"  ✓ #{doc_id} {title!r:50s}  → task {task_id[:8]}…")
                pushed += 1
            else:
                print(f"  ✗ #{doc_id} {title!r}: HTTP {r.status_code} {r.text[:120]}")
                failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ #{doc_id} {title!r}: {type(exc).__name__}: {exc}")
            failed += 1

    print(f"\nSummary: pushed={pushed} skipped={skipped} failed={failed}")
    if failed > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
