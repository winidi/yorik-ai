#!/usr/bin/env bash
# Install Yorik on a brand-new Ubuntu 24.04 VM, the way a stranger would,
# and check that it comes up. Nothing on this machine is touched except
# a cache of the Ubuntu cloud image and a throwaway VM directory.
#
#   bash scripts/test-fresh-install.sh              # --llm=none, then uninstall
#   LLM=ollama bash scripts/test-fresh-install.sh   # with the CPU model (+~7 GB, slower)
#   KEEP=1 bash scripts/test-fresh-install.sh       # leave the VM running for a look
#
# Needs: qemu-system-x86_64 + KVM, qemu-img, xorriso, ssh. The VM gets
# 12 GB RAM, 6 CPUs and an 80 GB sparse disk; ports 2222 (ssh) and 18000
# (Yorik) on 127.0.0.1 are forwarded to it.
#
# What it checks, in order: install.sh finishes without questions; Yorik
# answers /api/health; the first-run setup creates an admin; the Home
# checklist and help load; the update unit is installed; systemd runs
# Yorik; a reboot brings it back; uninstall.sh leaves no Yorik container.
# The result lands in e2e/history/install-<date>/ (log + summary).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/yorik-vm"
IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
BASE="$CACHE/noble-server-cloudimg-amd64.img"
LLM="${LLM:-none}"
EXTRA="${EXTRA:-}"   # more install.sh flags, e.g. EXTRA=--container
SSH_PORT="${SSH_PORT:-2222}"
WEB_PORT="${WEB_PORT:-18000}"
STAMP="$(date +%Y-%m-%d-%H%M)"
OUT="$REPO/e2e/history/install-$STAMP"
WORK="$(mktemp -d /tmp/yorik-vm.XXXXXX)"
mkdir -p "$CACHE" "$OUT"

RESULTS=()
pass() { RESULTS+=("PASS  $1"); printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { RESULTS+=("FAIL  $1"); printf "  \033[31m✗\033[0m %s\n" "$1"; }
step() { printf "\n\033[36m▸ %s\033[0m\n" "$1"; }

cleanup() {
  if [[ "${KEEP:-0}" == "1" ]]; then
    printf "\nVM kept: ssh -p %s -i %s/key yorik@127.0.0.1   Yorik: http://127.0.0.1:%s\n" "$SSH_PORT" "$WORK" "$WEB_PORT"
    printf "Stop it: kill \$(cat %s/qemu.pid); rm -rf %s\n" "$WORK" "$WORK"
    return
  fi
  [[ -f "$WORK/qemu.pid" ]] && kill "$(cat "$WORK/qemu.pid")" 2>/dev/null
  sleep 2; rm -rf "$WORK"
}
trap cleanup EXIT

for t in qemu-system-x86_64 qemu-img xorriso ssh ssh-keygen curl; do
  command -v "$t" >/dev/null || { echo "missing: $t"; exit 2; }
done
[[ -w /dev/kvm ]] || { echo "no access to /dev/kvm (add yourself to the kvm group)"; exit 2; }

step "Ubuntu 24.04 cloud image"
if [[ ! -f "$BASE" ]]; then
  curl -fL -C - --retry 3 -o "$BASE.partial" "$IMG_URL" && mv "$BASE.partial" "$BASE"
fi
[[ -f "$BASE" ]] && pass "base image cached at $BASE" || { fail "no base image"; exit 1; }

step "Throwaway VM"
qemu-img create -q -f qcow2 -F qcow2 -b "$BASE" "$WORK/disk.qcow2" 80G
ssh-keygen -q -t ed25519 -N "" -f "$WORK/key"
cat > "$WORK/user-data" <<EOF
#cloud-config
users:
  - name: yorik
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys: [$(cat "$WORK/key.pub")]
EOF
printf "instance-id: yorik-test-%s\nlocal-hostname: yorik-test\n" "$STAMP" > "$WORK/meta-data"
xorriso -as mkisofs -quiet -output "$WORK/seed.iso" -volid cidata -joliet -rock "$WORK/user-data" "$WORK/meta-data" 2>/dev/null
qemu-system-x86_64 -enable-kvm -cpu host -m 12G -smp 6 \
  -drive file="$WORK/disk.qcow2",if=virtio -drive file="$WORK/seed.iso",if=virtio,media=cdrom \
  -netdev user,id=n0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22,hostfwd=tcp:127.0.0.1:${WEB_PORT}-:8000 \
  -device virtio-net,netdev=n0 -display none -daemonize -pidfile "$WORK/qemu.pid" \
  -serial file:"$WORK/serial.log"
SSH=(ssh -p "$SSH_PORT" -i "$WORK/key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 yorik@127.0.0.1)
for _ in $(seq 1 90); do "${SSH[@]}" true 2>/dev/null && break; sleep 5; done
if "${SSH[@]}" "cloud-init status --wait >/dev/null; true" 2>/dev/null; then pass "VM up (Ubuntu 24.04, 12 GB, 6 CPUs)"; else fail "VM didn't come up (see $WORK/serial.log)"; exit 1; fi

step "Copy this checkout into the VM (as if cloned)"
tar -C "$REPO" --exclude=./venv --exclude=./data --exclude=./node_modules --exclude=./frontend-react/node_modules \
    --exclude=./e2e --exclude=./models --exclude=./infra/supabase/docker --exclude=./config.env \
    --exclude=./.install-record --exclude=./archive --exclude=./.git --exclude=./dist -czf "$WORK/src.tgz" .
cat "$WORK/src.tgz" | "${SSH[@]}" "mkdir -p ~/yorik-ai && tar -xzf - -C ~/yorik-ai" && pass "source copied ($(du -h "$WORK/src.tgz" | cut -f1))"

step "bash install.sh --llm=$LLM --no-tailscale $EXTRA  (no terminal: it must not ask anything)"
t0=$(date +%s)
"${SSH[@]}" "cd ~/yorik-ai && timeout 90m bash install.sh --llm=$LLM --no-tailscale $EXTRA </dev/null" > "$OUT/install.log" 2>&1
rc=$?
mins=$(( ($(date +%s) - t0) / 60 ))
if [[ $rc == 0 ]]; then pass "install.sh finished in ${mins} min"; else fail "install.sh exited $rc after ${mins} min (tail: $(tail -3 "$OUT/install.log" | tr '\n' ' '))"; fi
grep -q "Proceed? \[" "$OUT/install.log" && fail "install.sh asked a question" || pass "no questions asked"

step "Yorik answers"
curl -fsS --max-time 5 "http://127.0.0.1:$WEB_PORT/api/health" >/dev/null && pass "/api/health from outside the VM" || fail "/api/health unreachable"
me=$(curl -fsS "http://127.0.0.1:$WEB_PORT/api/auth/me" || echo '{}')
[[ "$me" == *'"setup_required":true'* ]] && pass "first run asks for the owner account" || fail "setup_required not true: ${me:0:120}"

step "First run: create the admin like the setup screen does"
JAR="$WORK/cookies"
code=$(curl -s -o "$WORK/setup.json" -w "%{http_code}" -c "$JAR" -H "content-type: application/json" \
  -d '{"name":"Test Admin","email":"admin@example.test","password":"test-install-2026"}' \
  "http://127.0.0.1:$WEB_PORT/api/auth/setup")
[[ "$code" == 200 || "$code" == 201 ]] && pass "owner account created" || fail "setup returned $code: $(head -c 200 "$WORK/setup.json")"
curl -fsS -b "$JAR" -c "$JAR" -H "content-type: application/json" -d '{"email":"admin@example.test","password":"test-install-2026"}' \
  "http://127.0.0.1:$WEB_PORT/api/auth/login" >/dev/null && pass "admin can sign in" || fail "admin sign-in failed"
curl -fsS -b "$JAR" "http://127.0.0.1:$WEB_PORT/api/setup/checklist" | grep -q '"steps"' && pass "Home checklist loads" || fail "checklist didn't load"
curl -fsS -b "$JAR" "http://127.0.0.1:$WEB_PORT/api/help" | grep -q '"topics"' && pass "in-app help loads" || fail "help didn't load"
curl -fsS "http://127.0.0.1:$WEB_PORT/r/home" | grep -q '<div id="root">' && pass "the app page is served" || fail "/r/home not served"

step "Services"
if [[ "$EXTRA" == *--container* ]]; then
  "${SSH[@]}" "sudo docker inspect -f '{{.State.Health.Status}}' yorik-app" 2>/dev/null | grep -q healthy \
    && pass "Yorik runs as the yorik-app container (healthy)" || fail "yorik-app container not healthy"
  "${SSH[@]}" "test ! -d ~/yorik-ai/venv" && pass "no Python venv on the host" || fail "a host venv exists in container mode"
else
  "${SSH[@]}" "systemctl is-active --quiet yorik" && pass "systemd runs yorik.service" || fail "yorik.service not active"
  "${SSH[@]}" "systemctl list-unit-files yorik-update.service | grep -q yorik-update" && pass "in-app update unit installed" || fail "yorik-update.service missing"
fi
"${SSH[@]}" "test -f ~/yorik-ai/.install-record" && pass "install record written" || fail "no .install-record"

step "Reboot"
"${SSH[@]}" "sudo reboot" 2>/dev/null; sleep 20
for _ in $(seq 1 60); do "${SSH[@]}" true 2>/dev/null && break; sleep 5; done
up=0
for _ in $(seq 1 60); do curl -fsS --max-time 3 "http://127.0.0.1:$WEB_PORT/api/health" >/dev/null 2>&1 && { up=1; break; }; sleep 5; done
[[ $up == 1 ]] && pass "Yorik back after a reboot" || fail "Yorik not back 5 min after reboot"

if [[ "${KEEP:-0}" != "1" ]]; then
  step "Uninstall"
  "${SSH[@]}" "cd ~/yorik-ai && echo yes | bash scripts/uninstall.sh" > "$OUT/uninstall.log" 2>&1
  left=$("${SSH[@]}" "sudo docker ps -a --format '{{.Names}}' | grep -ci -e yorik -e supabase -e immich -e paperless || true")
  [[ "${left:-0}" == "0" ]] && pass "uninstall left no Yorik containers" || fail "$left containers left after uninstall"
  "${SSH[@]}" "systemctl list-unit-files yorik.service | grep -q yorik.service" && fail "yorik.service still installed" || pass "yorik.service gone"
fi

{
  echo "# Fresh install test — $STAMP"
  echo
  echo "Ubuntu 24.04 cloud image, --llm=$LLM --no-tailscale $EXTRA, $(cd "$REPO" && git log -1 --format='%h %s')"
  echo
  for r in "${RESULTS[@]}"; do echo "- $r"; done
} > "$OUT/README.md"
fails=$(printf "%s\n" "${RESULTS[@]}" | grep -c '^FAIL' || true)
printf "\n%s: %s checks, %s failed — %s\n" "Fresh install" "${#RESULTS[@]}" "$fails" "$OUT/README.md"
exit $(( fails > 0 ? 1 : 0 ))
