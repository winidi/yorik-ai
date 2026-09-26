#!/usr/bin/env bash
# The all-in-one Docker install, end to end, on this machine, without
# touching anything that's already running: its own compose project
# (yorik-stacktest), throwaway volumes, ports only on 127.0.0.1
# (18080, 12283), no Tailscale sign-in, no model download.
#
#   bash scripts/test-docker-stack.sh              # build from this checkout
#   IMAGE_VERSION=edge bash scripts/test-docker-stack.sh   # pull a published image
#   KEEP=1 bash scripts/test-docker-stack.sh       # leave it running for a look
#
# Checks: images build/pull; every service starts; Yorik answers; the
# first-run owner account; sign-in; Yorik connects photos and documents
# by itself; help, checklist, an invite; a backup through the network
# path; the in-app update status. Result in e2e/history/docker-<date>/.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
P=yorik-stacktest
PORT=18080
STAMP="$(date +%Y-%m-%d-%H%M)"
OUT="$REPO/e2e/history/docker-$STAMP"
WORK="$(mktemp -d /tmp/yorik-stacktest.XXXXXX)"
mkdir -p "$OUT"
RESULTS=()
pass() { RESULTS+=("PASS  $1"); printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { RESULTS+=("FAIL  $1"); printf "  \033[31m✗\033[0m %s\n" "$1"; }
step() { printf "\n\033[36m▸ %s\033[0m\n" "$1"; }

r() { openssl rand -hex "${1:-24}"; }
cat > "$WORK/.env" <<EOF
YORIK_VERSION=${IMAGE_VERSION:-local}
TZ=Europe/Berlin
YORIK_PORT=127.0.0.1:$PORT
YORIK_PHOTOS_PORT=127.0.0.1:12283
YORIK_BACKUP_DIR=$WORK/backups
YORIK_DB_PASSWORD=$(r)
YORIK_JWT_SECRET=$(r 32)
IMMICH_DB_PASSWORD=$(r)
PAPERLESS_DB_PASSWORD=$(r)
PAPERLESS_SECRET_KEY=$(r 32)
PAPERLESS_ADMIN_PASSWORD=$(r 12)
PAPERLESS_YORIK_TOKEN=$(r)
YORIK_WA_BRIDGE_TOKEN=$(r)
YORIK_LLM_URL=http://no-model-in-this-test:1/v1
EOF
mkdir -p "$WORK/backups"; chmod 777 "$WORK/backups"
FILES=(-f compose.yaml)
[[ -z "${IMAGE_VERSION:-}" ]] && FILES+=(-f compose.build.yaml)
DC=(docker compose -p "$P" --env-file "$WORK/.env" "${FILES[@]}")
# The updater (Docker socket) and Tailscale (would want a sign-in) stay
# out of the test; everything else starts.
SERVICES=(yorik db immich-server immich-machine-learning immich-redis immich-postgres
          paperless-web paperless-db paperless-broker paperless-tika paperless-gotenberg whatsapp-bridge)

cleanup() {
  if [[ "${KEEP:-0}" == "1" ]]; then
    echo "kept: http://127.0.0.1:$PORT  — stop: cd $REPO/deploy && ${DC[*]} down -v; rm -rf $WORK"; return
  fi
  (cd "$REPO/deploy" && "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1)
  rm -rf "$WORK"
}
trap cleanup EXIT
cd "$REPO/deploy"

step "Images"
if [[ -z "${IMAGE_VERSION:-}" ]]; then
  "${DC[@]}" build --quiet yorik whatsapp-bridge > "$OUT/build.log" 2>&1 && pass "images built from this checkout" || { fail "image build failed (build.log)"; }
else
  "${DC[@]}" pull --quiet yorik whatsapp-bridge > "$OUT/pull.log" 2>&1 && pass "images ${IMAGE_VERSION} pulled" || fail "pull failed"
fi

step "Start"
"${DC[@]}" up -d --quiet-pull "${SERVICES[@]}" > "$OUT/up.log" 2>&1 && pass "all services started" || fail "compose up failed (up.log)"
up=0; for i in $(seq 1 120); do curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && { up=1; break; }; sleep 5; done
[[ $up == 1 ]] && pass "Yorik answers ($((i*5)) s)" || fail "Yorik didn't answer in 10 min"

step "First run"
B="http://127.0.0.1:$PORT"; J="$WORK/jar"
curl -s "$B/api/auth/me" | grep -q '"setup_required":true' && pass "asks for the owner account" || fail "setup_required not true"
curl -s -c "$J" -H "content-type: application/json" -d '{"name":"Test Admin","email":"owner@example.test","password":"stack-test-2026"}' "$B/api/auth/setup" | grep -q '"ok":true' \
  && pass "owner account created (local auth, no GoTrue)" || fail "setup failed"
curl -s -b "$J" -c "$J" -H "content-type: application/json" -d '{"email":"owner@example.test","password":"stack-test-2026"}' "$B/api/auth/login" | grep -q '"ok":true' \
  && pass "owner signs in" || fail "sign-in failed"

step "Yorik connects its neighbours"
for _ in $(seq 1 60); do
  logs=$("${DC[@]}" logs yorik 2>/dev/null)
  [[ "$logs" == *"documents: connected"* && "$logs" == *"photos: connected"* ]] && break
  sleep 10
done
[[ "$logs" == *"documents: connected"* ]] && pass "document archive connected" || fail "documents not connected"
[[ "$logs" == *"photos: connected"* ]] && pass "photo library connected" || fail "photos not connected"

step "Features"
curl -s -b "$J" "$B/api/help" | grep -q '"topics"' && pass "help" || fail "help"
curl -s -b "$J" "$B/api/setup/checklist" | grep -q '"steps"' && pass "Home checklist" || fail "checklist"
# (With a custom Host header curl matches cookies against that name,
# so the session cookie goes in by hand.)
SID=$(awk '$6=="yorik_session"{print $7}' "$J")
curl -s -H "Cookie: yorik_session=$SID" -H "content-type: application/json" -H "Host: 192.168.0.99:8000" -d '{"name":"Oma"}' "$B/api/invites" | grep -q '"join_url":"http://192.168.0.99:8000/r/join' \
  && pass "invite points at the home-network address" || fail "invite link"
curl -s -b "$J" -X PATCH -H "content-type: application/json" -d '{"target_path":"/backups","passphrase":"stack-test-passphrase"}' "$B/api/backup/config" >/dev/null
bk=$(curl -s -b "$J" -X POST "$B/api/backup/run")
[[ "$bk" == *'"ok":true'* && "$bk" == *yorik_postgres_main* ]] && pass "backup (database over the network)" || fail "backup: ${bk:0:160}"
ls "$WORK/backups"/*.age >/dev/null 2>&1 && pass "backup file on the host folder" || fail "no backup file in the host folder"
curl -s -b "$J" "$B/api/system/update" | grep -q '"runtime":"docker"' && pass "update status (docker)" || fail "update status"

"${DC[@]}" logs --no-color > "$OUT/stack.log" 2>&1
{
  echo "# Docker stack test — $STAMP"; echo
  echo "$(cd "$REPO" && git log -1 --format='%h %s'), images: ${IMAGE_VERSION:-built locally}"; echo
  for x in "${RESULTS[@]}"; do echo "- $x"; done
} > "$OUT/README.md"
fails=$(printf "%s\n" "${RESULTS[@]}" | grep -c '^FAIL' || true)
printf "\nDocker stack: %s checks, %s failed — %s\n" "${#RESULTS[@]}" "$fails" "$OUT/README.md"
exit $(( fails > 0 ? 1 : 0 ))
