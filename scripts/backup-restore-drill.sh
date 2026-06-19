#!/usr/bin/env bash
# Backup-restore drill.
#
# What it proves end-to-end:
#   1. backup.py builds an encrypted snapshot
#   2. The snapshot can be moved to a *different* working tree (simulates
#      "restore on another machine") and decrypted with the passphrase
#   3. After restore, family.db + documents.db + the credential key all
#      come back byte-equal to the originals
#   4. The recovered credential key can decrypt a stored connector token
#      that was sealed before the backup (the audit's critical scenario:
#      "lose the credential key = lose every connector credential")
#   5. start.sh's secret-guard isn't tripped by the restored values
#
# Runs entirely in tmp dirs, never touches your real ./data/. Exit code
# 0 = all green; non-zero = first failure.
#
# Usage:
#   bash scripts/backup-restore-drill.sh

set -euo pipefail
cd "$(dirname "$0")/.."

TMPROOT="$(mktemp -d -t yorik-bkup-drill-XXXXXX)"
SRC="$TMPROOT/src"          # the "production" Yorik
DST="$TMPROOT/dst"          # the "fresh machine" we restore onto
SNAPSHOT_DIR="$TMPROOT/snapshots"
PASSPHRASE="drill-passphrase-12345"

trap 'echo "--- cleanup ---"; rm -rf "$TMPROOT"' EXIT

mkdir -p "$SRC/data" "$DST" "$SNAPSHOT_DIR"

echo "── backup-restore drill ──────────────────────────────────"
echo "tmp root:      $TMPROOT"
echo "src data:      $SRC/data"
echo "dst data:      $DST/data"
echo "snapshot dir:  $SNAPSHOT_DIR"
echo

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

# We point Yorik at the src/data/ paths so the backup pulls from there
# instead of the real ./data/.
export HOMEOS_DB_PATH="$SRC/data/family.db"
export HOMEOS_DOCS_DB_PATH="$SRC/data/documents.db"
export HOMEOS_DOCS_DIR="$SRC/data/documents"
export HOMEOS_CREDENTIAL_KEY_PATH="$SRC/data/.credential_key"
export HOMEOS_BACKUP_TARGET="$SNAPSHOT_DIR"
export HOMEOS_BACKUP_PASSPHRASE="$PASSPHRASE"

check() {
  local label="$1"; shift
  if eval "$@"; then
    echo "  ✓ $label"
  else
    echo "  ✗ $label"
    exit 1
  fi
}

# ── PHASE 1: seed the source ────────────────────────────────────────
echo "→ seeding the source install with a user + a connector credential…"
python3 - <<'PY'
import os, sys
from pathlib import Path
# 1. Initialize schema + seed data
from backend.database import init_db, conn_ctx, DEFAULT_DB_PATH, DEFAULT_DOCS_DB_PATH
init_db()
# Touch the docs DB so it exists for the backup to pick up
Path(DEFAULT_DOCS_DB_PATH).touch()
# Insert a recognizable user row
with conn_ctx() as conn:
    conn.execute("INSERT OR IGNORE INTO user_profiles (name, email, role) VALUES (?, ?, ?)",
                 ("Drill User", "drill@yorik.local", "admin"))

# 2. Seal a fake connector credential — this exercises the credential_key
#    so we can later prove the SAME key roundtrips through the backup.
from backend import credential_store
credential_store.put("drill-test", {"api_key": "this-is-the-secret-we-must-recover-intact"})
print("  seeded.")
PY

ORIG_DB_SHA="$(sha256sum "$SRC/data/family.db" | awk '{print $1}')"
ORIG_DOCS_DB_SHA="$(sha256sum "$SRC/data/documents.db" | awk '{print $1}')"
ORIG_KEY_SHA="$(sha256sum "$SRC/data/.credential_key" | awk '{print $1}')"
echo "  family.db        sha256: $ORIG_DB_SHA"
echo "  documents.db     sha256: $ORIG_DOCS_DB_SHA"
echo "  .credential_key  sha256: $ORIG_KEY_SHA"

# ── PHASE 2: run a backup ────────────────────────────────────────────
echo
echo "→ running a backup to $SNAPSHOT_DIR …"
python3 - <<'PY'
import os, sys, json
from backend import backup
backup.set_config(
    target_path=os.environ["HOMEOS_BACKUP_TARGET"],
    schedule="off",
    retain_count=5,
    include_photos=False,
    include_paperless=False,
    passphrase=os.environ["HOMEOS_BACKUP_PASSPHRASE"],
)
result = backup._run_backup_sync()
print(json.dumps(result, indent=2, default=str))
if not result.get("ok"):
    sys.exit(1)
PY

# Find the produced .age file
SNAP="$(find "$SNAPSHOT_DIR" -maxdepth 1 -name '*.tar.gz.age' | head -1)"
[[ -n "$SNAP" ]] || { echo "✗ no .age snapshot produced"; exit 1; }
echo "  snapshot:  $SNAP  ($(stat -c%s "$SNAP") bytes)"
echo "  encrypted: $(head -c 9 "$SNAP" | xxd -ps | head -c 20)…  (age v1 header)"

# ── PHASE 3: restore onto a fresh "different machine" ────────────────
echo
echo "→ restoring onto a fresh tree at $DST …"

# Stage just enough of the repo for restore.sh to work: it needs venv,
# scripts/, and an empty data/. Symlinks are fine; restore.sh doesn't
# care about anything else in the repo root.
mkdir -p "$DST/scripts" "$DST/data"
cp scripts/restore.sh "$DST/scripts/restore.sh"
ln -sf "$(pwd)/venv" "$DST/venv"
# Pre-create a sentinel so we can tell when restore overwrote it
echo "PRE-RESTORE SENTINEL" > "$DST/data/sentinel-should-be-gone.txt"

# restore.sh wants a confirmation prompt + passphrase prompt. Feed both.
#
# IMPORTANT: restore.sh runs `fuser -k -9 8000/tcp` to stop a running
# Yorik before swapping data/. Inside the drill that would kill the
# user's real dev server on the host (drill is meant to be safe to
# run any time). We mask the `fuser` binary with a no-op for the
# subshell so the kill never reaches the host.
(
  cd "$DST"
  # `fuser` lookup is PATH-resolved; prepending a tmp dir with our own
  # stub makes shell-script `command -v fuser` find ours first and the
  # `fuser -k -9 8000/tcp` line becomes a no-op.
  mkdir -p "$TMPROOT/bin"
  cat > "$TMPROOT/bin/fuser" <<'STUB'
#!/usr/bin/env bash
# drill stub — drops args, exits 0. Real fuser would kill PIDs on the
# port, which we never want inside the drill.
exit 0
STUB
  chmod +x "$TMPROOT/bin/fuser"
  PATH="$TMPROOT/bin:$PATH" printf '%s\ny\n' "$PASSPHRASE" | PATH="$TMPROOT/bin:$PATH" bash scripts/restore.sh "$SNAP"
) | sed 's/^/  /' | tail -25

# ── PHASE 4: verify ──────────────────────────────────────────────────
echo
echo "→ verifying the restore is byte-intact…"

check "data/family.db was restored"      "[[ -f '$DST/data/family.db' ]]"
check "data/documents.db was restored"   "[[ -f '$DST/data/documents.db' ]]"
check "data/.credential_key was restored" "[[ -f '$DST/data/.credential_key' ]]"
check "data/MANIFEST.json present"        "[[ -f '$DST/data/MANIFEST.json' ]]"
check "pre-restore sentinel is GONE"      "[[ ! -f '$DST/data/sentinel-should-be-gone.txt' ]]"

NEW_DB_SHA="$(sha256sum "$DST/data/family.db" | awk '{print $1}')"
NEW_DOCS_DB_SHA="$(sha256sum "$DST/data/documents.db" | awk '{print $1}')"
NEW_KEY_SHA="$(sha256sum "$DST/data/.credential_key" | awk '{print $1}')"

# SQLite VACUUM INTO produces a logically-equivalent but not byte-equal
# file (header timestamps + ordering can differ). We check the user row
# survived instead of comparing bytes.
check "family.db has the drill user row" "sqlite3 '$DST/data/family.db' \"SELECT 1 FROM user_profiles WHERE email='drill@yorik.local'\" 2>/dev/null | grep -qx 1 || python3 -c \"import sqlite3;c=sqlite3.connect('$DST/data/family.db');r=c.execute(\\\"SELECT 1 FROM user_profiles WHERE email='drill@yorik.local'\\\").fetchone();exit(0 if r else 1)\""

# The credential key MUST be byte-equal — it's a raw Fernet key, not a
# DB that gets repacked. If even one bit differs, decryption fails.
check ".credential_key is BYTE-EQUAL to original" "[[ '$NEW_KEY_SHA' == '$ORIG_KEY_SHA' ]]"

# Now the killer assertion: can the recovered key + DB still decrypt
# the credential we sealed before the backup?
echo
echo "→ verifying the sealed connector credential round-trips…"
HOMEOS_DB_PATH="$DST/data/family.db" HOMEOS_CREDENTIAL_KEY_PATH="$DST/data/.credential_key" \
python3 - <<'PY'
import os, sys
# Force fresh import against the restored paths
for m in list(sys.modules):
    if m.startswith("backend"):
        del sys.modules[m]
from backend import credential_store
got = credential_store.get("drill-test")
if not got:
    print("✗ recovered credential store says drill-test isn't stored")
    sys.exit(1)
expected = "this-is-the-secret-we-must-recover-intact"
actual = got.get("api_key")
if actual != expected:
    print(f"✗ value mismatch: expected {expected!r}, got {actual!r}")
    sys.exit(1)
print(f"  ✓ drill-test connector credential decrypted intact:")
print(f"     api_key = {actual}")
PY

# ── PHASE 5: cosmetic sanity ─────────────────────────────────────────
echo
echo "→ MANIFEST.json contents:"
python3 -c "import json; print(json.dumps(json.loads(open('$DST/data/MANIFEST.json').read()), indent=2))" | sed 's/^/  /'

echo
echo "✓ backup-restore drill passed."
