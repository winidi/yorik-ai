# Sourced by install.sh for the default (Docker) install on Linux, once
# Docker and the source are in place. Uses install.sh's helpers (phase,
# ok, skip, warn, info, say, fatal, record, has_nvidia_gpu) and
# variables (INSTALL_DIR, INSTALL_USER). Windows and macOS do the same
# steps in deploy/install-windows.ps1 and deploy/install-mac.sh.

phase "Start Yorik (Docker)"
cd "$INSTALL_DIR/deploy"

# .env: generated once, never overwritten (it holds the passwords).
if [[ ! -f .env ]]; then
  r() { openssl rand -hex "${1:-24}"; }
  sed -e "s|^YORIK_DB_PASSWORD=.*|YORIK_DB_PASSWORD=$(r)|" \
      -e "s|^YORIK_JWT_SECRET=.*|YORIK_JWT_SECRET=$(r 32)|" \
      -e "s|^IMMICH_DB_PASSWORD=.*|IMMICH_DB_PASSWORD=$(r)|" \
      -e "s|^PAPERLESS_DB_PASSWORD=.*|PAPERLESS_DB_PASSWORD=$(r)|" \
      -e "s|^PAPERLESS_SECRET_KEY=.*|PAPERLESS_SECRET_KEY=$(r 32)|" \
      -e "s|^PAPERLESS_ADMIN_PASSWORD=.*|PAPERLESS_ADMIN_PASSWORD=$(r 12)|" \
      -e "s|^PAPERLESS_YORIK_TOKEN=.*|PAPERLESS_YORIK_TOKEN=$(r)|" \
      -e "s|^YORIK_WA_BRIDGE_TOKEN=.*|YORIK_WA_BRIDGE_TOKEN=$(r)|" \
      -e "s|^TZ=.*|TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo Europe/Berlin)|" \
      env.template > .env
  chmod 600 .env
  ok "settings created with fresh passwords (deploy/.env)"
else
  skip "deploy/.env exists — keeping its passwords"
fi

# --llm=none: no model in the stack (tests, or a model elsewhere later
# via Settings → LLM). Otherwise Ollama pulls YORIK_MODEL on first start.
if [[ "${FLAG_NO_LLM:-0}" == "1" ]] && ! grep -qE '^YORIK_LLM_URL=' .env; then
  printf "\nYORIK_LLM_URL=http://no-model-configured:1/v1\n" >> .env
  skip "no AI model (--llm=none); set one later in Settings → LLM"
fi

# Backups folder: the container runs as uid 1000 and writes here.
BACKUP_DIR="$(grep -E '^YORIK_BACKUP_DIR=' .env | cut -d= -f2-)"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$BACKUP_DIR"
sudo chown 1000:1000 "$BACKUP_DIR" 2>/dev/null || true

# Which compose files: GPU if there's one Docker can use, and a local
# build while no released images exist yet (or YORIK_BUILD=1). Stored
# as COMPOSE_FILE in .env, so plain `docker compose …` here and the
# in-app updater use the same set.
FILES="compose.yaml"
if has_nvidia_gpu && docker info 2>/dev/null | grep -qi 'runtimes:.*nvidia'; then
  FILES="$FILES:compose.gpu.yaml"; ok "the AI model will use the NVIDIA GPU"
fi
VERSION="$(grep -E '^YORIK_VERSION=' .env | cut -d= -f2-)"
if [[ "${YORIK_BUILD:-0}" == "1" ]] || ! docker manifest inspect "ghcr.io/winidi/yorik-ai:${VERSION:-stable}" >/dev/null 2>&1; then
  FILES="$FILES:compose.build.yaml"
  warn "no published Yorik image for '${VERSION:-stable}' — building it from this checkout (~10 min)"
fi
if grep -qE '^COMPOSE_FILE=' .env; then
  sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=${FILES}|" .env
else
  printf "\nCOMPOSE_FILE=%s\n" "$FILES" >> .env
fi
record runtime docker

say "starting Yorik, photos, documents, WhatsApp, the AI model and Tailscale"
info "the first start downloads several GB of images; later starts take seconds"
if [[ "$FILES" == *compose.build.yaml* ]]; then
  docker compose build --quiet yorik whatsapp-bridge || fatal "building the Yorik image failed" "see the output above"
fi
docker compose up -d --quiet-pull || fatal "docker compose up failed" "docker compose -f $INSTALL_DIR/deploy/compose.yaml logs"
ok "containers started"

phase "Wait for ready"
info "waiting for Yorik (first start fetches voice and search models too, up to 20 min)"
for _ in $(seq 1 240); do
  curl -fsS --max-time 2 http://localhost:8000/api/health >/dev/null 2>&1 && break
  sleep 5
done
curl -fsS --max-time 2 http://localhost:8000/api/health >/dev/null 2>&1 \
  && ok "Yorik is answering" \
  || warn "Yorik isn't answering yet — it may still be setting up: docker compose -f $INSTALL_DIR/deploy/compose.yaml logs -f yorik"

phase "Done"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
FINISH_URL="http://${LAN_IP:-localhost}:8000/r/"
printf "\n%sYorik is up.%s\n\n" "${BOLD}${GRN}" "${RST}"
if command -v qrencode >/dev/null 2>&1 && [[ -t 1 ]]; then
  printf "  %sScan this with your phone (same Wi-Fi) to finish setting up:%s\n\n" "${BOLD}" "${RST}"
  qrencode -t ansiutf8 -m 2 "$FINISH_URL" | sed 's/^/    /'
  echo
fi
cat <<EOF
  ${BOLD}Open${RST}    ${FINISH_URL}   (on this machine: http://localhost:8000)
  ${BOLD}On the go${RST}  Yorik → Settings → System → Phones: sign in to Tailscale once.
  ${BOLD}Logs${RST}    docker compose -f ${INSTALL_DIR}/deploy/compose.yaml logs -f yorik
  ${BOLD}Stop${RST}    docker compose -f ${INSTALL_DIR}/deploy/compose.yaml stop
  ${BOLD}Remove${RST}  bash ${INSTALL_DIR}/scripts/uninstall.sh

EOF
exit 0
