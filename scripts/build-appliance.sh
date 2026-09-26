#!/usr/bin/env bash
# Build a Yorik install stick: the official Ubuntu Server 24.04 image with
# this checkout and an unattended install on it. Written to a USB stick
# (balenaEtcher, Raspberry Pi Imager, `dd`) and booted on a mini PC, it
# wipes that PC's disk, installs Ubuntu, and on first start sets up
# Yorik by itself. The screen then shows a QR code to scan with a phone.
#
#   bash scripts/build-appliance.sh            → dist/yorik-appliance.iso
#   bash scripts/build-appliance.sh --test     … and install it on a blank VM,
#                                                 checking Yorik comes up
#
# Needs: xorriso, curl; for --test also qemu-system-x86_64 + KVM.
# The Ubuntu image is cached in ~/.cache/yorik-vm (2.7 GB, checksummed).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/yorik-vm"
ISO_NAME="ubuntu-24.04.5-live-server-amd64.iso"
ISO_SHA="97f3d7ffb032c3eb3b23d2c8be9cc76e60c2c1f2c0146ba5ba9fe01cafae0fd8"
BASE_ISO="$CACHE/$ISO_NAME"
OUT="$REPO/dist/yorik-appliance.iso"
WORK="$(mktemp -d /tmp/yorik-appliance.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$CACHE" "$REPO/dist"

say() { printf "\n\033[36m▸ %s\033[0m\n" "$1"; }

say "Ubuntu Server 24.04.5"
if [[ ! -f "$BASE_ISO" ]]; then
  curl -fL -C - --retry 5 -o "$BASE_ISO.partial" "https://releases.ubuntu.com/24.04/$ISO_NAME"
  mv "$BASE_ISO.partial" "$BASE_ISO"
fi
echo "$ISO_SHA  $BASE_ISO" | sha256sum -c --quiet - || { echo "checksum mismatch: $BASE_ISO"; exit 1; }

say "This checkout, as it will land on the box"
mkdir -p "$WORK/yorik"
tar -C "$REPO" --exclude=./venv --exclude=./data --exclude=./node_modules --exclude=./frontend-react/node_modules \
    --exclude=./e2e --exclude=./models --exclude=./infra/supabase/docker --exclude=./config.env --exclude=./dist \
    --exclude=./.install-record --exclude=./.yorik-runtime --exclude=./archive --exclude=./.git --exclude=./tests \
    -cf - . | tar -C "$WORK/yorik" -xf -
du -sh "$WORK/yorik" | awk '{print "  " $1 " of Yorik"}'

say "Boot menu: install without asking"
xorriso -osirrox on -indev "$BASE_ISO" -extract /boot/grub/grub.cfg "$WORK/grub.cfg" 2>/dev/null
chmod u+w "$WORK/grub.cfg"
# `autoinstall` on the kernel line skips the "are you sure" prompt; the
# stick's whole job is to install. Short timeout, first entry.
sed -i -e 's|---| autoinstall ---|' -e 's/^set timeout=.*/set timeout=5/' "$WORK/grub.cfg"
sed -i '0,/menuentry "Try or Install Ubuntu Server"/s//menuentry "Install Yorik (erases this computer'"'"'s disk)"/' "$WORK/grub.cfg"

say "Writing $OUT"
rm -f "$OUT"
xorriso -indev "$BASE_ISO" -outdev "$OUT" \
  -map "$REPO/appliance/autoinstall.yaml" /autoinstall.yaml \
  -map "$WORK/grub.cfg" /boot/grub/grub.cfg \
  -map "$WORK/yorik" /yorik \
  -boot_image any replay 2>&1 | grep -E "^xorriso : (UPDATE|NOTE).*(Writing|written)" || true
ls -lh "$OUT" | awk '{print "  " $5 "  " $9}'

if [[ "${1:-}" != "--test" ]]; then
  printf "\nWrite it to a USB stick, boot a PC from it, and wait for the QR code.\n"
  exit 0
fi

say "Test: install on a blank VM (this takes 30–60 minutes)"
DISK="$WORK/disk.qcow2"
qemu-img create -q -f qcow2 "$DISK" 80G
qemu-system-x86_64 -enable-kvm -cpu host -m 12G -smp 6 \
  -drive file="$DISK",if=virtio -cdrom "$OUT" -boot order=c,once=d \
  -netdev user,id=n0,hostfwd=tcp:127.0.0.1:${WEB_PORT:-18001}-:8000 -device virtio-net,netdev=n0 \
  -display none -serial file:"$WORK/serial.log" -daemonize -pidfile "$WORK/qemu.pid"
trap 'kill "$(cat "$WORK/qemu.pid" 2>/dev/null)" 2>/dev/null; sleep 2; rm -rf "$WORK"' EXIT
start=$(date +%s)
until curl -fsS --max-time 3 "http://127.0.0.1:${WEB_PORT:-18001}/api/health" >/dev/null 2>&1; do
  if (( $(date +%s) - start > 90 * 60 )); then
    echo "✗ Yorik didn't answer within 90 minutes (serial log: $WORK/serial.log)"; tail -20 "$WORK/serial.log"; exit 1
  fi
  sleep 30
done
printf "  \033[32m✓\033[0m blank disk → Ubuntu → Yorik answering in %d minutes\n" $(( ($(date +%s) - start) / 60 ))
me=$(curl -fsS "http://127.0.0.1:${WEB_PORT:-18001}/api/auth/me")
[[ "$me" == *'"setup_required":true'* ]] && printf "  \033[32m✓\033[0m waiting for its owner (first-run setup)\n" \
  || { echo "✗ unexpected /api/auth/me: ${me:0:160}"; exit 1; }
