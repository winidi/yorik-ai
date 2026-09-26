#!/usr/bin/env bash
# deploy/updater.sh, for real, on a tiny stand-in install: its own compose
# project (yorik-updtest), an alpine "app" with a folder mounted from the
# install folder, and the real updater. A fake release is served from
# this machine. Nothing else is touched.
#
#   bash scripts/test-updater.sh
#
# Checks: the new install files arrive, .env keeps its passwords and gets
# the new settings (secrets filled in), the app is recreated with its
# folders still pointing at the real install folder (not /deploy), the
# updater replaces itself, files stay owned by the user, and a broken
# download changes nothing and says why.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d /tmp/yorik-updtest.XXXXXX)"
INST="$WORK/My Yorik"          # a space, like "Application Support" on a Mac
SERVE="$WORK/releases"
PORT=18099
RESULTS=()
pass() { RESULTS+=("PASS  $1"); printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { RESULTS+=("FAIL  $1"); printf "  \033[31m✗\033[0m %s\n" "$1"; }
step() { printf "\n\033[36m▸ %s\033[0m\n" "$1"; }

compose_file() {  # $1 = marker for this "version"
  cat <<EOF
name: yorik-updtest
services:
  app:
    image: alpine:3.20
    command: sleep 100000
    environment: { VERSION_MARK: "$1", NEW_SETTING: "\${NEW_SETTING:-unset}" }
    volumes: [ "./appdata:/appdata", "\${YORIK_BACKUP_DIR:-./backups}:/backups" ]
  updater:
    image: docker:27-cli
    network_mode: host
    working_dir: /deploy
    entrypoint: ["sh", "/deploy/updater.sh"]
    environment: { YORIK_RELEASES_URL: "http://127.0.0.1:$PORT" }
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./:/deploy
      - updater:/updater
volumes:
  updater:
EOF
}
DC() { (cd "$INST" && docker compose "$@"); }
st() { docker exec yorik-updtest-updater-1 cat /updater/status 2>/dev/null; }
request_and_wait() {
  docker exec yorik-updtest-updater-1 sh -c 'echo requested > /updater/status; touch /updater/request'
  for _ in $(seq 1 40); do
    s=$(st); [[ "$s" == done* || "$s" == failed* ]] && return 0; sleep 3
  done; return 1
}
cleanup() {
  DC down -v --remove-orphans >/dev/null 2>&1
  [[ -n "${HTTP_PID:-}" ]] && kill "$HTTP_PID" 2>/dev/null
  # files written by containers are root's; remove them the same way
  docker run --rm -v "$WORK":/w alpine:3.20 sh -c 'rm -rf /w/* /w/.[!.]*' >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

step "An install of the old version"
mkdir -p "$INST/appdata" "$INST/backups" "$SERVE/latest/download" "$SERVE/download/v9.9.9"
compose_file old > "$INST/compose.yaml"
cp "$REPO/deploy/updater.sh" "$INST/updater.sh"
cat > "$INST/.env" <<EOF
YORIK_VERSION=stable
YORIK_DB_PASSWORD=keep-this-password
COMPOSE_FILE=compose.yaml
EOF
echo "photos" > "$INST/appdata/family.txt"
DC up -d --quiet-pull >/dev/null 2>&1 && pass "old version running" || fail "old version didn't start"

step "A new release"
NEW="$WORK/newbundle"; mkdir -p "$NEW/extra"
compose_file new > "$NEW/compose.yaml"
{ cat "$REPO/deploy/updater.sh"; echo "# new updater"; } > "$NEW/updater.sh"
printf 'YORIK_VERSION=stable\nYORIK_DB_PASSWORD=\nNEW_SETTING=hello\nNEW_SERVICE_TOKEN=\nTS_AUTHKEY=\n' > "$NEW/env.template"
echo "x" > "$NEW/extra/Yorik Setup.command"
echo "must not arrive" > "$NEW/.env"
(cd "$NEW" && zip -qr "$SERVE/latest/download/yorik-deploy.zip" . )
cp "$SERVE/latest/download/yorik-deploy.zip" "$SERVE/download/v9.9.9/"
(cd "$SERVE" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1) & HTTP_PID=$!
sleep 1

step "Update now"
started_before=$(docker inspect -f '{{.State.StartedAt}}' yorik-updtest-updater-1)
request_and_wait && [[ "$(st)" == done* ]] && pass "updater says done" || fail "updater: $(st)"
grep -q "VERSION_MARK: \"new\"" "$INST/compose.yaml" && pass "new compose.yaml arrived" || fail "compose.yaml not replaced"
[[ -f "$INST/extra/Yorik Setup.command" ]] && pass "file with a space in its name arrived" || fail "spaced file missing"
grep -q "^YORIK_DB_PASSWORD=keep-this-password$" "$INST/.env" && pass ".env kept its password" || fail ".env password changed"
! grep -q "must not arrive" "$INST/.env" && pass ".env not overwritten by the bundle" || fail ".env overwritten"
grep -q "^NEW_SETTING=hello$" "$INST/.env" && pass "new setting added to .env" || fail "new setting missing"
tok=$(grep "^NEW_SERVICE_TOKEN=" "$INST/.env" | cut -d= -f2)
[[ ${#tok} -ge 32 ]] && pass "new secret got a random value" || fail "new secret empty: '$tok'"
grep -q "^TS_AUTHKEY=$" "$INST/.env" && pass "TS_AUTHKEY left empty (not a generated secret)" || fail "TS_AUTHKEY got a value"
mark=$(docker inspect yorik-updtest-app-1 -f '{{range .Config.Env}}{{println .}}{{end}}' | grep VERSION_MARK)
[[ "$mark" == "VERSION_MARK=new" ]] && pass "app recreated as the new version" || fail "app still $mark"
[[ "$(docker inspect yorik-updtest-app-1 -f '{{range .Config.Env}}{{println .}}{{end}}' | grep NEW_SETTING)" == "NEW_SETTING=hello" ]] \
  && pass "app sees the new setting" || fail "app doesn't see NEW_SETTING"
src=$(docker inspect yorik-updtest-app-1 -f '{{range .Mounts}}{{if eq .Destination "/appdata"}}{{.Source}}{{end}}{{end}}')
[[ "$src" == "$INST/appdata" ]] && pass "app's folder still the real one ($src)" || fail "app's folder now: $src"
docker exec yorik-updtest-app-1 cat /appdata/family.txt 2>/dev/null | grep -q photos && pass "data still there" || fail "data gone"
[[ "$(stat -c %u "$INST/compose.yaml")" == "$(id -u)" ]] && pass "files still belong to the user" || fail "compose.yaml owned by $(stat -c %U "$INST/compose.yaml")"
for _ in $(seq 1 10); do
  docker inspect yorik-updtest-updater-1 >/dev/null 2>&1 && docker exec yorik-updtest-updater-1 grep -q "# new updater" /deploy/updater.sh 2>/dev/null && break; sleep 2
done
grep -q "# new updater" "$INST/updater.sh" && pass "updater replaced itself" || fail "updater.sh not replaced"
for _ in $(seq 1 10); do
  now=$(docker inspect -f '{{.State.StartedAt}}' yorik-updtest-updater-1 2>/dev/null)
  [[ -n "$now" && "$now" != "$started_before" ]] && break; sleep 2
done
[[ -n "$now" && "$now" != "$started_before" ]] && docker inspect -f '{{.State.Running}}' yorik-updtest-updater-1 | grep -q true \
  && pass "updater restarted with the new script" || fail "updater not restarted (still the old process)"
[[ "$(st)" == done* ]] && pass "status still says done after the restart" || fail "status after restart: $(st)"

step "A broken download"
echo "not a zip" > "$SERVE/latest/download/yorik-deploy.zip"
cp "$INST/compose.yaml" "$WORK/before.yaml"
sleep 3
request_and_wait; s=$(st)
[[ "$s" == failed* ]] && pass "says it failed: ${s% *}" || fail "status after broken download: $s"
cmp -s "$INST/compose.yaml" "$WORK/before.yaml" && pass "nothing changed" || fail "compose.yaml changed"
docker inspect -f '{{.State.Running}}' yorik-updtest-app-1 | grep -q true && pass "app kept running" || fail "app stopped"

step "A pinned version"
sed -i 's/^YORIK_VERSION=.*/YORIK_VERSION=9.9.9/' "$INST/.env"
request_and_wait; s=$(st)
[[ "$s" == done* ]] && pass "pinned 9.9.9 fetched from its own release" || fail "pinned: $s"

[[ -d /deploy && ! -O /deploy ]] && echo "  (note: a /deploy folder exists on this machine from an older updater; sudo rm -rf /deploy)"
fails=$(printf "%s\n" "${RESULTS[@]}" | grep -c '^FAIL' || true)
printf "\nUpdater: %s checks, %s failed\n" "${#RESULTS[@]}" "$fails"
exit $(( fails > 0 ? 1 : 0 ))
