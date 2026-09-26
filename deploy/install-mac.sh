#!/usr/bin/env bash
# Yorik for macOS (Apple silicon or Intel): sets up Docker Desktop if
# needed, starts the all-in-one stack (compose.yaml) and opens Yorik in
# the browser. In Terminal:
#
#   bash install-mac.sh            (or double-click "Yorik Setup.command")
#   bash install-mac.sh --source <checkout-or-deploy-folder> [--version <tag>]
#
# Not yet tested on a real Mac (the maintainer's test machines are Linux);
# the Linux and Windows paths are. Reports welcome.
#
# On a Mac the AI model is much faster outside Docker (Docker can't use
# the Apple GPU): if the Ollama app is installed, Yorik uses it instead of
# the model container.
set -euo pipefail
REPO="winidi/yorik-ai"
VERSION="stable"
SOURCE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done
HOME_="$HOME/Library/Application Support/Yorik"
BACKUPS="$HOME/Documents/Yorik Backups"
say()  { printf "  \033[36m>\033[0m %s\n" "$1"; }
ok()   { printf "  \033[32mv\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
fail() { printf "  \033[31mx\033[0m %s\n    %s\n" "$1" "${2:-}"; exit 1; }

printf "\n  Yorik for macOS\n\n"
[[ "$(uname)" == "Darwin" ]] || fail "this script is for macOS" "on Linux use install.sh, on Windows Yorik-Setup.cmd"
mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
(( mem_gb < 12 )) && warn "this Mac has ${mem_gb} GB of memory; Yorik runs best with 16 GB or more"

# ── Docker Desktop ────────────────────────────────────────────────────
if ! [[ -d "/Applications/Docker.app" ]]; then
  if command -v brew >/dev/null 2>&1; then
    say "installing Docker Desktop with Homebrew (free for personal use; its license applies)"
    brew install --cask docker
  else
    arch=$( [[ "$(uname -m)" == "arm64" ]] && echo arm64 || echo amd64 )
    say "downloading Docker Desktop (free for personal use; its license applies)"
    curl -fL -o /tmp/Docker.dmg "https://desktop.docker.com/mac/main/${arch}/Docker.dmg"
    hdiutil attach -quiet -nobrowse /tmp/Docker.dmg -mountpoint /tmp/docker-dmg
    cp -R "/tmp/docker-dmg/Docker.app" /Applications/
    hdiutil detach -quiet /tmp/docker-dmg; rm -f /tmp/Docker.dmg
  fi
  ok "Docker Desktop installed"
fi
if ! docker info >/dev/null 2>&1; then
  say "starting Docker Desktop (accept its terms if it asks)"
  open -a Docker
  for _ in $(seq 1 120); do docker info >/dev/null 2>&1 && break; sleep 5; done
  docker info >/dev/null 2>&1 || fail "Docker Desktop didn't start within 10 minutes" "open Docker once, then run this again"
fi
ok "Docker is running"

# ── files ─────────────────────────────────────────────────────────────
mkdir -p "$BACKUPS"
BUILD=0
if [[ -n "$SOURCE" && -f "$SOURCE/Dockerfile" ]] && ! docker manifest inspect "ghcr.io/winidi/yorik-ai:$VERSION" >/dev/null 2>&1; then
  BUILD=1; HOME_="$SOURCE/deploy"
  warn "no published image for '$VERSION' — building from the checkout (~10 min)"
elif [[ -n "$SOURCE" ]]; then
  src="$SOURCE"; [[ -f "$SOURCE/deploy/compose.yaml" ]] && src="$SOURCE/deploy"
  mkdir -p "$HOME_"; cp -R "$src/." "$HOME_/"
else
  mkdir -p "$HOME_"
  url="https://github.com/$REPO/releases/latest/download/yorik-deploy.zip"
  [[ "$VERSION" != "stable" ]] && url="https://github.com/$REPO/releases/download/$VERSION/yorik-deploy.zip"
  say "downloading Yorik ($VERSION)"
  curl -fL -o /tmp/yorik-deploy.zip "$url" || fail "couldn't download Yorik" "check the internet connection"
  unzip -oq /tmp/yorik-deploy.zip -d "$HOME_"; rm -f /tmp/yorik-deploy.zip
fi
cd "$HOME_"
if [[ ! -f .env ]]; then
  r() { openssl rand -hex "${1:-24}"; }
  tz=$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')
  sed -e "s|^YORIK_VERSION=.*|YORIK_VERSION=$VERSION|" -e "s|^TZ=.*|TZ=${tz:-Europe/Berlin}|" \
      -e "s|^YORIK_DB_PASSWORD=.*|YORIK_DB_PASSWORD=$(r)|" -e "s|^YORIK_JWT_SECRET=.*|YORIK_JWT_SECRET=$(r 32)|" \
      -e "s|^IMMICH_DB_PASSWORD=.*|IMMICH_DB_PASSWORD=$(r)|" -e "s|^PAPERLESS_DB_PASSWORD=.*|PAPERLESS_DB_PASSWORD=$(r)|" \
      -e "s|^PAPERLESS_SECRET_KEY=.*|PAPERLESS_SECRET_KEY=$(r 32)|" -e "s|^PAPERLESS_ADMIN_PASSWORD=.*|PAPERLESS_ADMIN_PASSWORD=$(r 12)|" \
      -e "s|^PAPERLESS_YORIK_TOKEN=.*|PAPERLESS_YORIK_TOKEN=$(r)|" -e "s|^YORIK_WA_BRIDGE_TOKEN=.*|YORIK_WA_BRIDGE_TOKEN=$(r)|" \
      -e "s|^YORIK_BACKUP_DIR=.*|YORIK_BACKUP_DIR=$BACKUPS|" \
      env.template > .env
  files="compose.yaml"; (( BUILD )) && files="$files:compose.build.yaml"
  printf "\nCOMPOSE_FILE=%s\n" "$files" >> .env
  # The Apple GPU is only reachable outside Docker: use the Ollama app if present.
  if [[ -d "/Applications/Ollama.app" ]] || command -v ollama >/dev/null 2>&1; then
    printf "YORIK_LLM_URL=http://host.docker.internal:11434/v1\n" >> .env
    ok "using the Ollama app for the AI model (fast on this Mac)"
    model=$(grep -E '^YORIK_MODEL=' .env | cut -d= -f2-)
    command -v ollama >/dev/null 2>&1 && { say "downloading the AI model into the Ollama app (several GB)"; ollama pull "$model" || warn "model download failed; retry with: ollama pull $model"; }
  fi
  chmod 600 .env
  ok "settings created with fresh passwords"
fi

# ── start ─────────────────────────────────────────────────────────────
say "starting Yorik (the first start downloads several GB)"
(( BUILD )) && docker compose build yorik whatsapp-bridge
docker compose up -d --quiet-pull
say "waiting for Yorik (first start: up to 20 minutes)"
for _ in $(seq 1 150); do curl -fsS --max-time 3 http://localhost:8000/api/health >/dev/null 2>&1 && break; sleep 10; done
curl -fsS --max-time 3 http://localhost:8000/api/health >/dev/null 2>&1 && ok "Yorik is answering" || warn "Yorik isn't answering yet; it may still be setting up"
open "http://localhost:8000/r/"
ip=$(ipconfig getifaddr en0 2>/dev/null || true)
printf "\n  Yorik is ready. Create your account in the browser.\n"
[[ -n "$ip" ]] && printf "  Phones at home: http://%s:8000\n" "$ip"
printf "  On the go: in Yorik, Settings → System → Phones → sign in to Tailscale.\n  Backups go to: %s\n\n" "$BACKUPS"
