#!/usr/bin/env bash
# Cold-install verification.
#
# Spins up Yorik against an isolated tmp DB + tmp doc store, then walks
# through the critical paths a brand-new user hits on first launch:
#
#   1. /api/auth/me               → expects setup_required=true
#   2. /api/auth/setup            → creates first admin (sets cookie)
#   3. /api/auth/me               → expects logged_in=true, onboarded_at=null
#   4. /api/profile (PATCH)       → onboarding wizard step
#   5. /api/onboarding/complete   → onboarded_at gets set
#   6. /api/compose/series/install-preset "de" → 4 series created
#   7. /api/system/status         → llm reachable + counts populated
#   8. /api/compose/templates     → at least 3 templates load
#   9. /api/quality/summary       → endpoint shape correct
#  10. /r/home                    → HTTP 200
#
# Runs against a separate port + DB so it never touches your real data.
# Exit code 0 = all checks green, non-zero = first failure.

set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${COLD_PORT:-8765}"
TMPROOT="$(mktemp -d -t yorik-cold-XXXXXX)"
trap 'echo "--- cleanup ---"; kill $UVI_PID 2>/dev/null || true; rm -rf "$TMPROOT"' EXIT

export HOMEOS_DB_PATH="$TMPROOT/family.db"
export HOMEOS_DOCS_DIR="$TMPROOT/docs"
export HOMEOS_DOCS_DB_PATH="$TMPROOT/documents.db"
# Cold-install verifies the SQLite happy path against an isolated tmp
# DB — the Postgres-backed install path is exercised by start.sh +
# the Phase F-lite tenant work, not by this script. Without forcing
# sqlite here, an operator who set YORIK_DB_BACKEND=postgres in
# config.env gets this script trying to spin a tenant FastAPI against
# the real Postgres cluster on a random port, which always fails on
# the connection pool and reports "uvicorn never came up" misleadingly.
# Also clear YORIK_DB_NAME so the new uvicorn doesn't try tenant mode.
export YORIK_DB_BACKEND=sqlite
unset YORIK_DB_NAME YORIK_IS_TENANT YORIK_HOST_INTERNAL_TOKEN_FILE
# Tell the bind-probe which port WE are actually binding (this script
# runs on a random non-default port). Without this, the probe checks
# YORIK_PORT (default 8000), sees the real host Yorik there, refuses
# to start, and we get "uvicorn never came up" misleadingly. HOMEOS_PORT
# is the same lookup the bind-probe respects per the recent Phase F-lite
# tenant fix.
export HOMEOS_PORT="$PORT"
mkdir -p "$HOMEOS_DOCS_DIR"

COOKIES="$TMPROOT/cookies.txt"
BASE="http://127.0.0.1:$PORT"

echo "── cold-install check ────────────────────────────────"
echo "tmp root:   $TMPROOT"
echo "tmp DB:     $HOMEOS_DB_PATH"
echo "port:       $PORT"
echo

# Need venv active for uvicorn. Caller can also set VIRTUAL_ENV already.
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

echo "→ launching uvicorn against the fresh DB…"
uvicorn backend.main:app --host 127.0.0.1 --port "$PORT" >/tmp/yorik-cold.log 2>&1 &
UVI_PID=$!

# wait for /api/health
for _i in {1..30}; do
  if curl -fs "$BASE/api/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
if ! curl -fs "$BASE/api/health" >/dev/null 2>&1; then
  echo "✗ uvicorn never came up — see /tmp/yorik-cold.log"
  exit 1
fi
echo "  uvicorn live."

check() {
  local label="$1"; shift
  if eval "$@"; then
    echo "  ✓ $label"
  else
    echo "  ✗ $label"
    exit 1
  fi
}

echo
echo "→ /api/auth/me on a virgin install:"
RESP="$(curl -fs "$BASE/api/auth/me")"
check "setup_required = true" "echo '$RESP' | python3 -c 'import json,sys;d=json.load(sys.stdin); sys.exit(0 if d.get(\"setup_required\") and not d.get(\"logged_in\") else 1)'"

echo
echo "→ Auth guard: every protected /api/* path must 401 without a cookie:"
# This is the core security invariant the audit caught. If this regresses,
# someone removed the middleware or the whitelist swallowed too much.
for path in /api/events /api/tasks /api/bills /api/connectors/credentialed \
            /api/quality/summary /api/system/status /api/compose/templates \
            /api/compose/series /api/users /api/notifications; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")"
  check "$path → 401 anonymously"        "[[ $code == 401 ]]"
done
# And tampering attempts shouldn't get a different answer:
for q in "?role=admin" "?role=child" "?role=anything"; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/events$q")"
  check "/api/events$q → 401"             "[[ $code == 401 ]]"
done
# Whitelisted public endpoints stay reachable:
for path in /api/health /api/auth/me /api/openapi.json; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")"
  check "$path → 200 (whitelisted)"      "[[ $code == 200 ]]"
done

echo
echo "→ /api/auth/setup (create the first admin):"
RESP="$(curl -fs -c "$COOKIES" -X POST "$BASE/api/auth/setup" \
  -H 'content-type: application/json' \
  -d '{"name":"Test Admin","email":"test@yorik.local","password":"cold123456"}')"
check "setup returns ok=true with user_id" "echo '$RESP' | python3 -c 'import json,sys;d=json.load(sys.stdin); sys.exit(0 if d.get(\"ok\") and d.get(\"user_id\") else 1)'"

echo
echo "→ /api/auth/me after setup:"
RESP="$(curl -fs -b "$COOKIES" "$BASE/api/auth/me")"
check "logged in"        "echo '$RESP' | python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin).get(\"logged_in\") else 1)'"
check "onboarded_at null" "echo '$RESP' | python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin).get(\"user\",{}).get(\"onboarded_at\") is None else 1)'"

echo
echo "→ /api/profile PATCH (sim onboarding wizard):"
RESP="$(curl -fs -b "$COOKIES" -X PATCH "$BASE/api/profile" \
  -H 'content-type: application/json' \
  -d '{"country":"DE","language":"de","address_street":"Teststr. 1","address_city":"Berlin","address_postcode":"10115","business_name":"ColdTest GmbH","tax_id":"DE111222333"}')"
check "profile saved"   "echo '$RESP' | python3 -c 'import json,sys;d=json.load(sys.stdin); sys.exit(0 if d.get(\"country\")==\"DE\" and d.get(\"business_name\")==\"ColdTest GmbH\" else 1)'"

echo
echo "→ /api/onboarding/complete:"
curl -fs -b "$COOKIES" -X POST "$BASE/api/onboarding/complete" >/dev/null
RESP="$(curl -fs -b "$COOKIES" "$BASE/api/auth/me")"
check "onboarded_at now set" "echo '$RESP' | python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin).get(\"user\",{}).get(\"onboarded_at\") else 1)'"

echo
echo "→ /api/compose/series/install-preset (DE pack):"
RESP="$(curl -fs -b "$COOKIES" -X POST "$BASE/api/compose/series/install-preset?role=admin" \
  -H 'content-type: application/json' -d '{"preset":"de"}')"
check "4 series created" "echo '$RESP' | python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin).get(\"count\")==4 else 1)'"

echo
echo "→ /api/system/status:"
RESP="$(curl -fs -b "$COOKIES" "$BASE/api/system/status?role=admin")"
check "llm.reachable present"   "echo '$RESP' | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if \"reachable\" in d.get(\"llm\",{}) else 1)'"
check "counts present"          "echo '$RESP' | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get(\"counts\",{}).get(\"numbering_series\")==4 else 1)'"

echo
echo "→ /api/compose/templates:"
RESP="$(curl -fs -b "$COOKIES" "$BASE/api/compose/templates?role=admin")"
# Fresh installs ship 1 bundled template: generic-letter. (The carpenter +
# praxis examples used to sit here as "reference apps" but were removed —
# a fresh user shouldn't see them. AI-generated and user-added templates
# accumulate on top.)
check "≥1 template loaded" "echo '$RESP' | python3 -c 'import json,sys;sys.exit(0 if len(json.load(sys.stdin))>=1 else 1)'"

echo
echo "→ /api/quality/summary:"
RESP="$(curl -fs -b "$COOKIES" "$BASE/api/quality/summary?role=admin&days=30")"
check "current_model in summary" "echo '$RESP' | python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin).get(\"current_model\") else 1)'"

echo
echo "→ /r/home:"
CODE="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/r/home")"
check "HTTP 200" "[[ $CODE == 200 ]]"

echo
echo "✓ all cold-install checks passed."
