#!/usr/bin/env bash
# scripts/seed-test-data.sh — thin wrapper around scripts/seed-test-data.py
# so contributors don't have to remember the python path / activate the venv.
#
# Two-phase by design:
#   fetch — generates / downloads test data into data/seed-cache/ (idempotent)
#   seed  — uploads the cached files into the running Paperless/Immich/Yorik
#
# Workstation: run `fetch all` once. The cache survives in
# data/seed-cache/ (gitignored). For dev VMs, rsync the cache over OR
# re-run `fetch` (Unsplash photos burn API quota; CONTACTS and DOCS are
# offline-generated so re-fetching them on the VM is free).
#
# Usage:
#   bash scripts/seed-test-data.sh fetch all
#   bash scripts/seed-test-data.sh seed all
#   bash scripts/seed-test-data.sh status
#
# Environment:
#   UNSPLASH_ACCESS_KEY    needed for `fetch photos` only
#   YORIK_SEED_CONTACTS    contact count (default 100)
#   YORIK_SEED_PHOTOS      photo count (default 200)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

# Pick the right Python — prefer the local venv (has backend deps so the
# seed step can call into backend.contacts_import etc.), else system.
if [[ -x "$ROOT/venv/bin/python" ]]; then
  PY="$ROOT/venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

exec "$PY" "$SCRIPT_DIR/seed-test-data.py" "$@"
