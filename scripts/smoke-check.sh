#!/usr/bin/env bash
# scripts/smoke-check.sh — is this Yorik install actually working?
#
# Walks the paths a household hits on day one against a RUNNING Yorik:
# health, front door, login, a task through the REST API, a delete
# staged by the chat model and confirmed on the card, the WhatsApp
# bridge gate, a backup verify of the newest snapshot. Read-only except
# for one task it creates and deletes under the account it is given.
#
# Usage:
#   bash scripts/smoke-check.sh                       # http://localhost:8000, prompts for login
#   YORIK_URL=http://box:8000 YORIK_SMOKE_EMAIL=me@x YORIK_SMOKE_PASSWORD=… bash scripts/smoke-check.sh
#   bash scripts/smoke-check.sh --no-llm              # skip the chat round-trip (no model running)
#
# Exit 0 = every check green. Run it on a fresh install, after
# `yorik upgrade`, and before tagging a release.

set -uo pipefail
cd "$(dirname "$0")/.."

URL="${YORIK_URL:-http://localhost:8000}"
EMAIL="${YORIK_SMOKE_EMAIL:-}"
PASSWORD="${YORIK_SMOKE_PASSWORD:-}"
WITH_LLM=1
for arg in "$@"; do
  case "$arg" in
    --no-llm) WITH_LLM=0 ;;
    --help|-h) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

if [[ -t 1 ]]; then GRN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; RST=$'\033[0m'; else GRN=""; RED=""; YEL=""; RST=""; fi
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf "  %s✓%s %s\n" "$GRN" "$RST" "$1"; }
bad()  { FAIL=$((FAIL+1)); printf "  %s✗%s %s\n" "$RED" "$RST" "$1"; [[ -n "${2:-}" ]] && printf "      %s\n" "$2"; }
skip() { printf "  %s·%s %s\n" "$YEL" "$RST" "$1"; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
COOKIES="$TMP/cookies"
py() { python3 -c "$1"; }

echo "Yorik smoke check → $URL"

# 1. health ──────────────────────────────────────────────────────────
HEALTH="$(curl -fsS -m 8 "$URL/api/health" 2>/dev/null)" || { bad "GET /api/health" "not reachable — is Yorik running? (bash start.sh)"; }
if [[ -n "${HEALTH:-}" ]]; then
  echo "$HEALTH" | py 'import sys,json; d=json.load(sys.stdin); assert d.get("status")=="ok"' 2>/dev/null && ok "health: status ok" || bad "health: status not ok" "$HEALTH"
  echo "$HEALTH" | grep -q '"database"' && ok "health: database $(echo "$HEALTH" | py 'import sys,json; print(json.load(sys.stdin).get("database"))')" || bad "health: no database field"
  echo "$HEALTH" | grep -q '"credential_key": *"ok"' && ok "health: credential key present" || bad "health: credential key MISSING — restore data/.credential_key"
  LLM_OK="$(echo "$HEALTH" | py 'import sys,json; print("1" if json.load(sys.stdin).get("llm_reachable") else "0")')"
  if [[ "$LLM_OK" == "1" ]]; then ok "health: LLM reachable ($(echo "$HEALTH" | py 'import sys,json; d=json.load(sys.stdin); print(d.get("model"), "@", d.get("base_url"))'))"
  else skip "health: LLM not reachable — chat checks will be skipped"; WITH_LLM=0; fi
fi

# 2. front door ──────────────────────────────────────────────────────
CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 8 "$URL/")"; LOC="$(curl -s -o /dev/null -w '%{redirect_url}' -m 8 "$URL/")"
[[ "$CODE" == "307" && "$LOC" == */r/home ]] && ok "/ → /r/home" || bad "/ should redirect to /r/home" "got $CODE $LOC"
curl -fsS -m 8 "$URL/r/home" 2>/dev/null | grep -q '<div id="root">' && ok "/r/home serves the React app" || bad "/r/home does not serve the React app"
CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 8 "$URL/api/openapi.json")"
[[ "$CODE" == "200" ]] && ok "/api/openapi.json reachable" || bad "/api/openapi.json → $CODE"

# 3. login ───────────────────────────────────────────────────────────
ME="$(curl -fsS -m 8 "$URL/api/auth/me" 2>/dev/null)"
if echo "$ME" | grep -q '"setup_required": *true'; then
  bad "first-run setup not done" "open $URL and create the admin account, then re-run"
  echo; echo "$PASS passed, $FAIL failed"; exit 1
fi
if [[ -z "$EMAIL" ]]; then read -rp "  login email: " EMAIL; fi
if [[ -z "$PASSWORD" ]]; then read -rsp "  password: " PASSWORD; echo; fi
LOGIN="$(curl -s -m 10 -c "$COOKIES" -X POST "$URL/api/auth/login" -H 'Content-Type: application/json' \
         -d "$(py "import json; print(json.dumps({'email': '$EMAIL', 'password': '''$PASSWORD'''}))")")"
if echo "$LOGIN" | grep -q '"ok": *true'; then ok "login as $EMAIL"; else bad "login failed" "$LOGIN"; echo; echo "$PASS passed, $FAIL failed"; exit 1; fi
ROLE="$(curl -fsS -m 8 -b "$COOKIES" "$URL/api/auth/me" | py 'import sys,json; print((json.load(sys.stdin).get("user") or {}).get("role"))')"
ok "session works (role: $ROLE)"

# 4. a task through the REST API ─────────────────────────────────────
TITLE="Smoke check $(date +%s)"
TASK="$(curl -s -m 10 -b "$COOKIES" -X POST "$URL/api/tasks" -H 'Content-Type: application/json' -d "{\"title\": \"$TITLE\"}")"
TASK_ID="$(echo "$TASK" | py 'import sys,json; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)"
[[ -n "$TASK_ID" ]] && ok "POST /api/tasks created #$TASK_ID" || bad "POST /api/tasks" "$TASK"

# 5. chat: stage a delete, confirm it on the card ────────────────────
if [[ "$WITH_LLM" == "1" && -n "$TASK_ID" ]]; then
  ASK="$(curl -s -m 240 -b "$COOKIES" -X POST "$URL/api/ask" -H 'Content-Type: application/json' \
         -d "$(py "import json; print(json.dumps({'message': 'Lösche die Aufgabe $TITLE'}))")")"
  PID="$(echo "$ASK" | py 'import sys,json; d=json.load(sys.stdin); c=[a for a in d.get("ui_actions") or [] if a.get("type")=="pending_confirmation"]; print(c[0]["pending_id"] if c else "")' 2>/dev/null)"
  if [[ -n "$PID" ]]; then
    ok "chat staged the delete (card shown, model said: $(echo "$ASK" | py 'import sys,json; print((json.load(sys.stdin).get("response") or "")[:70].replace("\n"," "))')…)"
    STILL="$(curl -s -m 8 -b "$COOKIES" "$URL/api/tasks" | py "import sys,json; d=json.load(sys.stdin); items=d if isinstance(d,list) else d.get('tasks') or d.get('items') or []; print('1' if any(t.get('id')==$TASK_ID for t in items) else '0')")"
    [[ "$STILL" == "1" ]] && ok "task still exists before confirmation" || bad "task vanished before the user confirmed"
    CONF="$(curl -s -m 20 -b "$COOKIES" -X POST "$URL/api/pending/$PID/confirm")"
    echo "$CONF" | grep -q '"applied": *"delete_task"' && ok "card Delete removed the task" || bad "confirm did not apply" "$CONF"
    TASK_ID=""
  else
    bad "chat did not stage a delete card" "$(echo "$ASK" | head -c 300)"
  fi
else
  skip "chat round-trip skipped (--no-llm or LLM unreachable)"
fi
if [[ -n "$TASK_ID" ]]; then
  curl -s -m 8 -b "$COOKIES" -X DELETE "$URL/api/tasks/$TASK_ID" >/dev/null && ok "cleanup: task #$TASK_ID deleted via REST" || bad "cleanup: DELETE /api/tasks/$TASK_ID"
fi

# 6. WhatsApp bridge gate ────────────────────────────────────────────
BRIDGE="${YORIK_WA_BRIDGE_URL:-http://127.0.0.1:3015}"
if curl -s -o /dev/null -m 3 "$BRIDGE/users" 2>/dev/null; then
  CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$BRIDGE/users")"
  [[ "$CODE" == "401" ]] && ok "bridge refuses requests without the token" || bad "bridge answered $CODE without a token — YORIK_WA_BRIDGE_TOKEN not set?"
  WA="$(curl -s -m 8 -b "$COOKIES" "$URL/api/whatsapp/status")"
  echo "$WA" | grep -q '"connected"' && ok "backend reaches the bridge ($(echo "$WA" | py 'import sys,json; d=json.load(sys.stdin); print("paired" if d.get("connected") else "not paired for this user")'))" || bad "GET /api/whatsapp/status" "$WA"
else
  skip "no WhatsApp bridge on $BRIDGE"
fi

# 7. newest backup snapshot verifies ─────────────────────────────────
if [[ -x venv/bin/python ]]; then
  SNAP="$(ls -1t data/backups/*.tar.gz.age 2>/dev/null | head -1)"
  if [[ -n "$SNAP" && -n "${HOMEOS_BACKUP_PASSPHRASE:-}" ]]; then
    if venv/bin/python scripts/yorik backup-verify "$SNAP" >/dev/null 2>&1; then ok "backup-verify $(basename "$SNAP")"; else bad "backup-verify failed for $(basename "$SNAP")"; fi
  else
    skip "backup verify skipped (no snapshot in data/backups or HOMEOS_BACKUP_PASSPHRASE unset)"
  fi
fi

echo
if (( FAIL == 0 )); then printf "%s%d checks passed.%s\n" "$GRN" "$PASS" "$RST"; exit 0
else printf "%s%d passed, %d failed.%s\n" "$RED" "$PASS" "$FAIL" "$RST"; exit 1; fi
