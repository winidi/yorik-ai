#!/usr/bin/env bash
# Yorik — one-shot installer for a fresh Linux box.
#
# Supported: Ubuntu 24.04+ / Debian 12+ / Fedora 39+ (also works inside
# WSL2 Ubuntu — see the warning printed if WSL is detected).
# macOS uses a separate path — see docs/INSTALL.md for the brew route.
#
# What it does:
#   1. Pre-flight checks — OS, RAM, disk, ports, network. Fail fast.
#   2. One question (or skip with --yes).
#   3. System packages (apt or dnf).
#   4. Docker (if missing).
#   5. LLM — detect existing endpoint, OR install Ollama + R7, OR install
#      llama.cpp:server-cuda + the unsloth Qwen3.5-9B GGUF if you have
#      an NVIDIA GPU.
#   6. Clone Yorik (if not already in a clone), write config.env.
#   7. Run start.sh — Python venv, voice models, Supabase stack, FastAPI.
#   8. Wait for /api/health, optional cold-install smoke.
#   9. Optional: systemd autostart.
#  10. Security checklist.
#
# Re-runnable: every step short-circuits when its work is already done.
#
# Flags:
#   --yes         Accept all defaults. No prompts.
#   --no-llm      Skip the LLM install (you point Yorik at your own).
#   --llm=ollama  Force the Ollama + R7 path (default when no GPU).
#   --llm=cuda    Force llama.cpp:server-cuda (default when NVIDIA GPU
#                 is detected). Fails if no GPU.
#   --llm=existing  Use a chat-capable LLM already running on a
#                   common local port (8080 / 11434 / 1234 / 8081 / 5000).
#   --llm=remote=URL  Use a chat-capable LLM on another host (e.g.
#                     http://10.0.0.5:8080/v1). The endpoint is probed
#                     with a tiny /v1/chat/completions request; install
#                     fails fast if it can't speak chat.
#   --dir=PATH    Install Yorik here. Default: current dir if already
#                 in a clone, otherwise $HOME/yorik.
#   --help        This message.

set -Eeuo pipefail

INSTALL_USER="$(id -un)"

# ─── helpers ──────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YEL=$'\033[33m'; CYA=$'\033[36m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YEL=""; CYA=""; RST=""
fi

PHASE_START_TS=0
phase() {
  if (( PHASE_START_TS > 0 )); then
    local elapsed=$(( $(date +%s) - PHASE_START_TS ))
    printf "%s  done (%ds)%s\n\n" "${DIM}" "$elapsed" "${RST}"
  fi
  PHASE_START_TS=$(date +%s)
  printf "%s━━ %s ━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n" "${BOLD}${CYA}" "$1" "${RST}"
}
ok()    { printf "  %s✓%s %s\n" "${GRN}" "${RST}" "$1"; }
skip()  { printf "  %s·%s %s\n" "${YEL}" "${RST}" "$1"; }
warn()  { printf "  %s⚠%s %s\n" "${YEL}" "${RST}" "$1"; }
info()  { printf "  %s    %s%s\n" "${DIM}" "$1" "${RST}"; }
say()   { printf "  %s▸%s %s\n" "${CYA}" "${RST}" "$1"; }
fatal() {
  printf "%s  ✗ %s%s\n" "${RED}" "$1" "${RST}" >&2
  [[ -n "${2:-}" ]] && printf "%s    fix: %s%s\n" "${DIM}" "$2" "${RST}" >&2
  exit 1
}

# ─── flag parsing ─────────────────────────────────────────────────────
FLAG_YES=0
FLAG_NO_LLM=0
FLAG_LLM=""
FLAG_REMOTE_LLM_URL=""
FLAG_DIR=""
FLAG_FULL=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y)        FLAG_YES=1 ;;
    --no-llm)        FLAG_NO_LLM=1 ;;
    --llm=ollama)    FLAG_LLM="ollama" ;;
    --llm=cuda)      FLAG_LLM="cuda" ;;
    --llm=existing)  FLAG_LLM="existing" ;;
    --llm=remote=*)  FLAG_LLM="remote"; FLAG_REMOTE_LLM_URL="${arg#--llm=remote=}" ;;
    --llm=*)         fatal "unknown --llm value: ${arg#--llm=}" "use --llm=ollama|cuda|existing or --llm=remote=URL" ;;
    --dir=*)         FLAG_DIR="${arg#--dir=}" ;;
    --full)          FLAG_FULL=1 ;;
    --help|-h)
      sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) fatal "unknown flag: $arg" "see --help" ;;
  esac
done

# ─── pre-flight checks ────────────────────────────────────────────────
phase "Pre-flight checks"

if [[ "$INSTALL_USER" == "root" ]]; then
  fatal "don't run install.sh as root" \
        "use your normal user — sudo is invoked for the parts that need it"
fi
if ! command -v sudo >/dev/null 2>&1; then
  fatal "sudo is required (installs packages and a systemd unit)"
fi
if ! sudo -n true 2>/dev/null; then
  info "sudo will prompt for your password during install"
fi

# OS check
if [[ "$(uname -s)" == "Darwin" ]]; then
  fatal "macOS isn't supported by install.sh yet" \
        "see docs/INSTALL.md for the brew + launchd path"
fi
if [[ ! -f /etc/os-release ]]; then
  fatal "/etc/os-release missing — can't detect distribution"
fi
. /etc/os-release
OS_ID="${ID:-unknown}"
OS_VERSION_ID="${VERSION_ID:-0}"

case "$OS_ID" in
  ubuntu)
    if (( ${OS_VERSION_ID%%.*} < 24 )); then
      fatal "Ubuntu ${OS_VERSION_ID} — Yorik requires 24.04 or newer" \
            "Python 3.12 + cgroups v2 baseline; upgrade with sudo do-release-upgrade"
    fi
    ok "Ubuntu ${OS_VERSION_ID}"; PKG_MGR="apt" ;;
  debian)
    if (( ${OS_VERSION_ID%%.*} < 12 )); then
      fatal "Debian ${OS_VERSION_ID} — Yorik requires 12 (Bookworm) or newer"
    fi
    ok "Debian ${OS_VERSION_ID}"; PKG_MGR="apt" ;;
  fedora)
    if (( OS_VERSION_ID < 39 )); then
      fatal "Fedora ${OS_VERSION_ID} — Yorik requires 39 or newer"
    fi
    ok "Fedora ${OS_VERSION_ID}"; PKG_MGR="dnf" ;;
  pop|linuxmint)
    ok "$PRETTY_NAME (Ubuntu-derived, proceeding)"; PKG_MGR="apt" ;;
  *)
    fatal "unsupported OS: ${PRETTY_NAME:-$OS_ID $OS_VERSION_ID}" \
          "Yorik supports Ubuntu 24+, Debian 12+, Fedora 39+. macOS via brew." ;;
esac

# WSL detection — works fine but warn about the /mnt/c gotcha
if grep -qi microsoft /proc/version 2>/dev/null; then
  warn "WSL2 detected"
  info "install Yorik under \$HOME, NOT /mnt/c — cross-FS performance is brutal for Postgres"
  info "enable systemd in /etc/wsl.conf so the service auto-starts"
fi

# RAM ≥ 8 GB
RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
RAM_GB=$(( RAM_KB / 1024 / 1024 ))
if (( RAM_GB < 8 )); then
  fatal "RAM is ${RAM_GB} GB — Yorik needs at least 8 GB (16 GB recommended)" \
        "the LLM alone wants 4-6 GB at runtime; below 8 you'll swap on every chat"
fi
ok "${RAM_GB} GB RAM"

# Disk ≥ 30 GB free. On a re-run inside an existing clone we soften
# this to ≥ 5 GB — the clone has already paid the model download tax,
# so the residual budget needed is small. Without this softening,
# a half-failed first install gets the user stuck because their disk
# is now 'too full' to retry.
TARGET_PARENT="${FLAG_DIR:-$HOME}"
TARGET_PARENT_DIR="$(dirname "$TARGET_PARENT")"
[[ -d "$TARGET_PARENT_DIR" ]] || TARGET_PARENT_DIR="$HOME"
DISK_FREE_KB=$(df -k "$TARGET_PARENT_DIR" | awk 'NR==2 {print $4}')
DISK_FREE_GB=$(( DISK_FREE_KB / 1024 / 1024 ))
DISK_NEED_GB=30
if [[ -f "./start.sh" && -f "./config.env.example" ]]; then
  DISK_NEED_GB=5
fi
if (( DISK_FREE_GB < DISK_NEED_GB )); then
  fatal "${DISK_FREE_GB} GB free at ${TARGET_PARENT_DIR} — Yorik needs ≥ ${DISK_NEED_GB} GB" \
        "model alone is ~6 GB, Postgres + voice models + photos grow fast"
fi
ok "${DISK_FREE_GB} GB free at ${TARGET_PARENT_DIR}"

# Decide planned backend (Postgres/Supabase by default; SQLite if the
# operator pre-set it in config.env before install). The backend
# choice drives which ports we need free.
PLANNED_BACKEND=$(grep -E "^YORIK_DB_BACKEND=" config.env 2>/dev/null \
                  | head -1 | cut -d= -f2- | tr -d '[:space:]' \
                  || echo postgres)

# Port pre-flight. Every port Yorik's bundled stack needs to bind
# gets checked here. Failing now beats producing a half-up Supabase
# stack three minutes into the install. We name the typical
# offender for each port so the operator knows which container to
# stop first. Bundled compose containers from a PREVIOUS Yorik
# install are detected separately so we don't tell the user to
# free a port they already own.
#
# Skipped: 5435 (supabase-db) and 8400 (Kong HTTP) — those are
# Yorik-owned shifts; if a previous Yorik attempt left them taken,
# the compose layer handles the conflict by re-attaching.
_port_holder() {
  # Print a one-line description of who's on the given port, or
  # nothing if the port is free.
  local port="$1"
  if ! ss -lnt "sport = :$port" 2>/dev/null | grep -q LISTEN; then
    return 0
  fi
  local holder
  holder=$(docker ps --format '{{.Names}} ({{.Image}})' 2>/dev/null \
           | xargs -I{} sh -c "docker port \$(echo {} | awk '{print \$1}') 2>/dev/null | grep -q \"->\$port\" && echo {}" \
           2>/dev/null | head -1)
  if [[ -n "$holder" ]]; then
    echo "  $holder"
  else
    echo "  (process not in docker — try \`sudo ss -lntp 'sport = :$port'\`)"
  fi
}

# Always required.
REQUIRED_PORTS=("8000:Yorik FastAPI")
if [[ "$PLANNED_BACKEND" == "postgres" ]]; then
  REQUIRED_PORTS+=(
    "5432:Supavisor session pooler"
    "6543:Supavisor transaction pooler"
    "8453:Supabase Kong HTTPS"
    "2283:Immich web UI"
    "8010:Paperless web UI"
    "3015:WhatsApp bridge"
  )
fi

PORT_BUSY=0
for entry in "${REQUIRED_PORTS[@]}"; do
  port="${entry%%:*}"
  desc="${entry#*:}"
  if ss -lnt "sport = :$port" 2>/dev/null | grep -q LISTEN; then
    holder=$(_port_holder "$port")
    warn "port $port is taken (needed for $desc)"
    [[ -n "$holder" ]] && printf "%s\n" "$holder"
    PORT_BUSY=1
  fi
done
if (( PORT_BUSY )); then
  fatal "one or more required ports are in use" \
        "free the ports above (stop the noted container or process) and re-run install.sh"
fi
ok "all required ports free"

# Docker is required for the default Postgres/Supabase backend. We can
# install Docker later in the script if it's missing (Phase 4) — so we
# don't hard-fail here, we just announce the requirement. If the operator
# explicitly opts into legacy SQLite mode (config.env override before
# install), Docker is optional.
if [[ "$PLANNED_BACKEND" == "postgres" ]]; then
  if command -v docker >/dev/null 2>&1; then
    ok "docker available (default Postgres/Supabase backend)"
  else
    info "docker missing — will be installed in Phase 4 (required for Supabase)"
  fi
fi

# Network reachability — needed for apt + Docker + Ollama downloads
if ! curl -fsSL --max-time 5 https://github.com >/dev/null 2>&1; then
  fatal "github.com unreachable" "check DNS/firewall before continuing"
fi
ok "network reachable"

# Clock-skew check. apt-get update refuses to install from release
# files dated in the future. VMs restored from old snapshots and boxes
# whose NTP never synced both hit this — the failure mode is opaque
# ("InRelease is not valid yet"), so catch it explicitly and offer a
# one-line remedy.
HTTP_DATE=$(curl -sI --max-time 5 https://github.com 2>/dev/null \
  | awk -F': ' '/^[Dd]ate: / {sub(/\r$/, "", $2); print $2; exit}')
if [[ -n "$HTTP_DATE" ]]; then
  REMOTE_EPOCH=$(date -d "$HTTP_DATE" +%s 2>/dev/null || echo 0)
  LOCAL_EPOCH=$(date +%s)
  SKEW=$(( REMOTE_EPOCH > LOCAL_EPOCH ? REMOTE_EPOCH - LOCAL_EPOCH : LOCAL_EPOCH - REMOTE_EPOCH ))
  if (( REMOTE_EPOCH > 0 && SKEW > 600 )); then
    fatal "system clock is off by $((SKEW / 60))m vs. internet time" \
          "sudo timedatectl set-ntp true && sudo systemctl restart systemd-timesyncd"
  fi
fi
ok "system clock within tolerance"

# ─── decide the plan ──────────────────────────────────────────────────
phase "Plan"

has_nvidia_gpu() {
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L 2>/dev/null | grep -q GPU
}

_is_obvious_embedder() {
  # Cheap pre-filter so we don't waste a chat-completion probe call
  # on names that are clearly embedding-only models. Matched against
  # the lowercased model id.
  case "${1,,}" in
    *embed*|*bge-*|*-e5-*|nomic-*|*gte-*|*reranker*) return 0 ;;
  esac
  return 1
}

_chat_probe() {
  # POST a 1-token chat completion to confirm the (url, model) pair is
  # a chat LLM (not an embedder masquerading as one via /v1/models).
  # Echoes the model id on success + returns 0; returns 1 silently
  # otherwise. 5s timeout is generous; a cold model load on a small
  # box still fits.
  local url="$1" model="$2" probe
  probe=$(curl -fsS --max-time 5 -X POST "${url}/chat/completions" \
            -H 'Content-Type: application/json' \
            -d "$(printf '{"model":"%s","messages":[{"role":"user","content":"hi"}],"max_tokens":1,"stream":false}' "$model")" \
            2>/dev/null \
          | jq -r '.choices[0].message.content // empty' 2>/dev/null \
          || true)
  [[ -n "$probe" ]] || return 1
  printf '%s\n' "$model"
}

_chat_capable_model_for() {
  # Given a base URL, return the first model id that passes the chat
  # probe — skipping obvious embedders along the way. Empty + exit 1
  # if the URL is unreachable or no model is chat-capable.
  local url="$1" models model
  models=$(curl -fsS --max-time 2 "${url}/models" 2>/dev/null \
           | jq -r '.data[]?.id // empty' 2>/dev/null) || return 1
  [[ -n "$models" ]] || return 1
  while read -r model; do
    [[ -n "$model" ]] || continue
    _is_obvious_embedder "$model" && continue
    if _chat_probe "$url" "$model" >/dev/null; then
      printf '%s\n' "$model"
      return 0
    fi
  done <<< "$models"
  return 1
}

detect_existing_llm() {
  # Probe each common local port. For each one that answers /v1/models,
  # walk the model list and chat-probe until we find one that actually
  # serves chat completions. Echoes "<url>\t<model>" and returns 0 on
  # success.
  #
  # Without the chat probe, an Ollama install that's only pulled an
  # embedder (nomic-embed-text:latest) would get silently configured
  # as Yorik's chat LLM — every /api/ask call then 400s at first run.
  local port url model
  for port in 8080 11434 1234 8081 5000; do
    url="http://127.0.0.1:${port}/v1"
    curl -fsS --max-time 1 "${url}/models" >/dev/null 2>&1 || continue
    if model="$(_chat_capable_model_for "$url")"; then
      printf '%s\t%s\n' "$url" "$model"
      return 0
    fi
  done
  return 1
}

# Interactive: prompt the operator for a remote LLM URL + chat-capable
# model. Probes the URL, lists candidate models, lets them pick one
# (or auto-picks if only one passes the probe). Sets globals
# DETECTED_LLM_URL + DETECTED_LLM_MODEL on success.
_prompt_remote_llm() {
  local url cleaned models candidates n choice picked
  while true; do
    printf "  Remote LLM base URL (must speak OpenAI /v1/chat/completions): "
    read -r url || return 1
    [[ -z "$url" ]] && return 1
    # Tolerate the operator pasting host:port without scheme + with
    # or without trailing /v1 — normalise to <scheme>://<host>:<port>/v1
    cleaned="${url%/}"
    [[ "$cleaned" =~ ^https?:// ]] || cleaned="http://${cleaned}"
    [[ "$cleaned" =~ /v1$ ]] || cleaned="${cleaned%/v1}/v1"
    cleaned="${cleaned%/}"
    if ! curl -fsS --max-time 4 "${cleaned}/models" >/dev/null 2>&1; then
      printf "  ${RED}✗${RST} Can't reach %s/models — try again or Ctrl-C to skip.\n" "$cleaned"
      continue
    fi
    # Build a candidate list: every model that's not an obvious
    # embedder + passes the chat probe.
    models=$(curl -fsS --max-time 3 "${cleaned}/models" 2>/dev/null \
             | jq -r '.data[]?.id // empty' 2>/dev/null)
    candidates=()
    while read -r m; do
      [[ -n "$m" ]] || continue
      _is_obvious_embedder "$m" && continue
      if _chat_probe "$cleaned" "$m" >/dev/null; then
        candidates+=("$m")
      fi
    done <<< "$models"
    n=${#candidates[@]}
    if (( n == 0 )); then
      printf "  ${RED}✗${RST} %s reachable, but no chat-capable model found.\n" "$cleaned"
      printf "    (We probed every non-embedder model; none accepted /v1/chat/completions.)\n"
      continue
    fi
    if (( n == 1 )); then
      picked="${candidates[0]}"
      printf "  ${GRN}✓${RST} chat probe ok — using model ${BOLD}%s${RST}\n" "$picked"
    else
      echo "  Chat-capable models on this endpoint:"
      for ((i=0; i<n; i++)); do
        printf "    %d) %s\n" "$((i+1))" "${candidates[$i]}"
      done
      while true; do
        printf "  Pick a model [1-%d]: " "$n"
        read -r choice || return 1
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= n )); then
          picked="${candidates[$((choice-1))]}"; break
        fi
      done
    fi
    DETECTED_LLM_URL="$cleaned"
    DETECTED_LLM_MODEL="$picked"
    return 0
  done
}

DECIDED_LLM="$FLAG_LLM"
[[ "$FLAG_NO_LLM" == "1" ]] && DECIDED_LLM="none"

DETECTED_LLM_URL=""
DETECTED_LLM_MODEL=""

# --llm=remote=http://... was parsed earlier into FLAG_REMOTE_LLM_URL.
# Honour it now with the same chat-probe validation we use for
# interactive remote URLs.
if [[ "$DECIDED_LLM" == "remote" ]] && [[ -n "${FLAG_REMOTE_LLM_URL:-}" ]]; then
  _model="$(_chat_capable_model_for "${FLAG_REMOTE_LLM_URL%/}")" \
    || fatal "--llm=remote=${FLAG_REMOTE_LLM_URL} — endpoint unreachable or no chat-capable model" \
             "verify the URL serves /v1/chat/completions and at least one non-embedder model"
  DETECTED_LLM_URL="${FLAG_REMOTE_LLM_URL%/}"
  DETECTED_LLM_MODEL="$_model"
fi

if [[ -z "$DECIDED_LLM" ]]; then
  # Auto/interactive path. First probe the local box for an existing
  # chat-capable endpoint so we can offer it as the default.
  if _det="$(detect_existing_llm)"; then
    DETECTED_LLM_URL="${_det%%$'\t'*}"
    DETECTED_LLM_MODEL="${_det##*$'\t'}"
  fi

  # Embedder-only probe for the warning. detect_existing_llm already
  # rejected embedder-only endpoints, so DETECTED_LLM_URL is empty if
  # the only thing running is e.g. Ollama with just nomic-embed-text.
  # Surface that loudly so the operator knows they need to either pull
  # a chat model OR point Yorik at a different endpoint.
  _embedder_only_url=""
  _embedder_only_model=""
  if [[ -z "$DETECTED_LLM_URL" ]]; then
    for _port in 8080 11434 1234 8081 5000; do
      _u="http://127.0.0.1:${_port}/v1"
      curl -fsS --max-time 1 "${_u}/models" >/dev/null 2>&1 || continue
      _m=$(curl -fsS --max-time 2 "${_u}/models" 2>/dev/null \
           | jq -r '.data[0].id // empty' 2>/dev/null)
      [[ -n "$_m" ]] || continue
      _embedder_only_url="$_u"
      _embedder_only_model="$_m"
      break
    done
  fi

  if [[ "$FLAG_YES" == "1" ]]; then
    if [[ -n "$DETECTED_LLM_URL" ]]; then
      DECIDED_LLM="existing"
    else
      DECIDED_LLM="none"
      [[ -n "$_embedder_only_url" ]] && \
        warn "found LLM at $_embedder_only_url (model: $_embedder_only_model) but it failed the chat probe — skipping"
      info "no chat-capable LLM auto-detected — configure one later via Settings → LLM"
    fi
  else
    # Interactive prompt. Show what we found (or didn't), give the
    # operator a clear menu including the "I have a remote box" path
    # they came in wanting.
    echo
    if [[ -n "$DETECTED_LLM_URL" ]]; then
      printf "  %sWe detected a chat-capable LLM running locally:%s\n" "${BOLD}" "${RST}"
      printf "    %s  (model: %s — chat probe ${GRN}✓${RST})\n\n" "$DETECTED_LLM_URL" "$DETECTED_LLM_MODEL"
    elif [[ -n "$_embedder_only_url" ]]; then
      printf "  %sWe detected an LLM running locally:%s\n" "${BOLD}" "${RST}"
      printf "    %s  (model: %s)\n" "$_embedder_only_url" "$_embedder_only_model"
      printf "    ${YEL}⚠${RST} It failed the chat-capability probe — looks like an embedder.\n"
      printf "       Yorik chat would not work against this endpoint.\n\n"
    else
      printf "  %sNo chat-capable LLM found on common local ports.%s\n\n" "${BOLD}" "${RST}"
    fi
    printf "  How would you like to set up Yorik's chat LLM?\n\n"
    [[ -n "$DETECTED_LLM_URL" ]] && \
      printf "    [u] Use the detected LLM above\n"
    printf "    [r] Remote — I have an LLM on another box; paste the URL\n"
    if has_nvidia_gpu; then
      printf "    [l] Local — install one here (CUDA + Qwen 3.5 9B, ~6 GB)\n"
    else
      printf "    [l] Local — no NVIDIA GPU; we'll skip the recommended Qwen and you\n"
      printf "        wire up your own endpoint later in Settings\n"
    fi
    printf "    [s] Skip — set it up later in Settings → LLM\n"
    if [[ -n "$DETECTED_LLM_URL" ]]; then
      _default="u"
      _prompt="  Choice [U/r/l/s]: "
    else
      _default="s"
      _prompt="  Choice [r/l/${_default^^}]: "
    fi
    while true; do
      printf "%s" "$_prompt"
      read -r _choice
      _choice="${_choice:-$_default}"
      case "${_choice,,}" in
        u)
          [[ -n "$DETECTED_LLM_URL" ]] || { printf "    No detected LLM to use — try again.\n"; continue; }
          DECIDED_LLM="existing"; break ;;
        r)
          if _prompt_remote_llm; then DECIDED_LLM="remote"; break
          else printf "    Skipping remote.\n"; continue
          fi ;;
        l)
          if has_nvidia_gpu; then DECIDED_LLM="cuda"
          else DECIDED_LLM="none"
            info "no NVIDIA GPU — skipping local LLM install; configure later via Settings → LLM"
          fi
          break ;;
        s) DECIDED_LLM="none"; break ;;
        *) printf "    Pick u/r/l/s.\n"; continue ;;
      esac
    done
  fi
fi

if [[ "$DECIDED_LLM" == "existing" && -z "$DETECTED_LLM_URL" ]]; then
  if _det="$(detect_existing_llm)"; then
    DETECTED_LLM_URL="${_det%%$'\t'*}"
    DETECTED_LLM_MODEL="${_det##*$'\t'}"
  else
    fatal "--llm=existing but no chat-capable LLM detected on common ports" \
          "start a chat LLM first, or use --llm=remote=URL / drop the flag for the interactive menu"
  fi
fi

case "$DECIDED_LLM" in
  existing) LLM_LINE="use existing LLM at ${DETECTED_LLM_URL} (model: ${DETECTED_LLM_MODEL})" ;;
  remote)   LLM_LINE="use remote LLM at ${DETECTED_LLM_URL} (model: ${DETECTED_LLM_MODEL})" ;;
  cuda)     LLM_LINE="install llama.cpp:server-cuda + unsloth/Qwen3.5-9B-GGUF (Q5_K_M, ~7 GB) + vision projector" ;;
  ollama)   LLM_LINE="install Ollama + robit/qwen3.5-9b-r7-research-vision:q4km (~6.3 GB, vision works)" ;;
  none)     LLM_LINE="skip LLM install (you'll point Yorik at one later)" ;;
esac

INSTALL_DIR="${FLAG_DIR:-$HOME/yorik}"
if [[ -f "./start.sh" && -f "./config.env.example" ]]; then
  INSTALL_DIR="$(pwd)"
  IN_CLONE=1
else
  IN_CLONE=0
fi

printf "\n%sPlan:%s\n" "${BOLD}" "${RST}"
if (( IN_CLONE )); then
  printf "  • Install in place at %s%s%s\n" "${BOLD}" "$INSTALL_DIR" "${RST}"
else
  printf "  • Clone Yorik into %s%s%s\n" "${BOLD}" "$INSTALL_DIR" "${RST}"
fi
printf "  • System packages via %s + Docker if missing\n" "$PKG_MGR"
printf "  • LLM: %s\n" "$LLM_LINE"
printf "  • Start Yorik on http://localhost:8000\n\n"

if [[ "$FLAG_YES" != "1" ]]; then
  read -r -p "Proceed? [Y/n] " resp
  case "${resp,,}" in
    n|no) info "aborted"; exit 0 ;;
    *)    ;;
  esac
fi

# ─── system packages ──────────────────────────────────────────────────
phase "System packages"

# Freshly-booted Ubuntu boxes run unattended-upgrades shortly after
# login. It holds the dpkg lock for a few minutes; bare apt-get fails
# with a verbose "Could not get lock" message. Wait it out instead of
# erroring — first-time users on a new VM are exactly who hits this.
wait_for_dpkg_lock() {
  local waited=0 max=300
  while sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
     || sudo fuser /var/lib/apt/lists/lock        >/dev/null 2>&1 \
     || sudo fuser /var/lib/dpkg/lock             >/dev/null 2>&1; do
    if (( waited == 0 )); then
      info "another apt process is running (unattended-upgrades?) — waiting up to 5 min"
    fi
    sleep 3
    waited=$((waited + 3))
    if (( waited >= max )); then
      fatal "dpkg lock still held after 5 minutes" \
            "sudo systemctl disable --now unattended-upgrades, then re-run install.sh"
    fi
  done
}

if [[ "$PKG_MGR" == "apt" ]]; then
  wait_for_dpkg_lock
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    git curl ca-certificates \
    python3 python3-venv python3-pip \
    ffmpeg sqlite3 jq \
    iproute2 \
    >/dev/null
elif [[ "$PKG_MGR" == "dnf" ]]; then
  sudo dnf install -y -q \
    git curl ca-certificates \
    python3 python3-pip python3-virtualenv \
    ffmpeg-free sqlite jq \
    iproute \
    >/dev/null
fi
ok "git curl python3 ffmpeg sqlite3 jq"

# ─── Docker ───────────────────────────────────────────────────────────
phase "Docker"

DOCKER_PREFIX=""
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  skip "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo present)"
else
  say "installing Docker via get.docker.com"
  curl -fsSL https://get.docker.com | sudo sh >/dev/null
  sudo usermod -aG docker "$INSTALL_USER" || true
  ok "Docker installed"
fi

# Buildx plugin — silences the "Docker Compose requires buildx plugin
# to be installed" warning that surfaces during Phase 6 when the
# Yorik docker-compose tries to build the whatsapp-bridge image.
# Compose's legacy builder still works, but the warning looks ugly
# in install transcripts. Best-effort install — the warning is
# cosmetic, so we don't fail the install if buildx can't be added.
if ! docker buildx version >/dev/null 2>&1; then
  say "installing docker-buildx-plugin (silences a compose warning)"
  # Two package names depending on which repo provided docker:
  #   - get.docker.com installs from Docker's own repo → docker-buildx-plugin
  #   - Ubuntu's universe ships it as           → docker-buildx
  if [[ "$PKG_MGR" == "apt" ]]; then
    wait_for_dpkg_lock
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-buildx-plugin >/dev/null 2>&1 \
      || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-buildx >/dev/null 2>&1 \
      || warn "docker-buildx-plugin not installable from apt — compose will warn but still build (legacy builder)"
  elif [[ "$PKG_MGR" == "dnf" ]]; then
    sudo dnf install -y -q docker-buildx >/dev/null 2>&1 \
      || warn "docker-buildx not installable from dnf — compose will warn but still build (legacy builder)"
  fi
  docker buildx version >/dev/null 2>&1 && ok "buildx ready" || true
fi

# Use docker without sudo if possible; otherwise sg docker -c "..." so
# we don't force a logout/relogin inside the install run.
if docker ps >/dev/null 2>&1; then
  :
elif sg docker -c "docker ps" >/dev/null 2>&1; then
  DOCKER_PREFIX='sg docker -c '
  info "docker group not yet active — wrapping docker calls in 'sg docker -c'"
  warn "after install: log out + back in (or 'newgrp docker') so future shells work without the wrapper"
else
  fatal "Docker is installed but can't be invoked" \
        "log out + back in (or 'newgrp docker'), then re-run install.sh"
fi

# ─── LLM ──────────────────────────────────────────────────────────────
phase "LLM"

LLM_BASE_URL=""
LLM_MODEL=""

case "$DECIDED_LLM" in
  existing|remote)
    # Both detected-local and operator-supplied-remote paths already
    # ran the chat probe earlier and stashed the validated URL+model
    # in DETECTED_LLM_URL + DETECTED_LLM_MODEL. Trust those — no need
    # to re-query /v1/models here (the old code re-picked .data[0].id
    # blindly and stomped on the chat-capable model the operator just
    # picked when the endpoint also serves an embedder).
    LLM_BASE_URL="$DETECTED_LLM_URL"
    LLM_MODEL="$DETECTED_LLM_MODEL"
    if [[ "$DECIDED_LLM" == "remote" ]]; then
      ok "using remote LLM at ${LLM_BASE_URL} (model: ${LLM_MODEL})"
    else
      ok "using existing LLM at ${LLM_BASE_URL} (model: ${LLM_MODEL})"
    fi
    ;;

  cuda)
    if ! has_nvidia_gpu; then
      fatal "--llm=cuda but no NVIDIA GPU detected" \
            "drop the flag to fall back to Ollama, or check 'nvidia-smi'"
    fi
    MODEL_DIR="$INSTALL_DIR/models/qwen3.5-9b"
    # Yorik may not be cloned yet; the model dir lives next to the
    # eventual install. Pre-create here so the download below has
    # somewhere to write.
    mkdir -p "$MODEL_DIR"

    say "pulling llama.cpp:server-cuda Docker image (~3 GB, one-time)"
    if [[ -n "$DOCKER_PREFIX" ]]; then
      eval "$DOCKER_PREFIX 'docker pull ghcr.io/ggml-org/llama.cpp:server-cuda'" >/dev/null
    else
      docker pull ghcr.io/ggml-org/llama.cpp:server-cuda >/dev/null
    fi
    ok "image pulled"

    # Q5_K_M intentionally — Q4_K_M's embedded jinja chat template
    # raise_exception()s on a second system message ("System message
    # must be at the beginning"), which breaks every chat turn whose
    # per-conversation entity ledger is non-empty (i.e. every multi-
    # step Yorik chat). Q5_K_M ships a newer template with explicit
    # merged_system handling for two back-to-back system messages.
    # 7 GB instead of 5 GB; the unsloth team's recommended quant for
    # Yorik's pattern.
    GGUF_URL="https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q5_K_M.gguf"
    # The repo's mmproj file is `mmproj-F16.gguf`, not the model-prefixed
    # name we use locally. Saving it locally with the model prefix is
    # fine (just the filename downstream cares about).
    MMPROJ_URL="https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/mmproj-F16.gguf"

    if [[ ! -f "$MODEL_DIR/Qwen3.5-9B-Q5_K_M.gguf" ]]; then
      say "downloading Qwen3.5-9B Q5_K_M (~7 GB) — this is the long step"
      curl -fL --progress-bar -o "$MODEL_DIR/Qwen3.5-9B-Q5_K_M.gguf.partial" "$GGUF_URL"
      mv "$MODEL_DIR/Qwen3.5-9B-Q5_K_M.gguf.partial" "$MODEL_DIR/Qwen3.5-9B-Q5_K_M.gguf"
    else
      skip "model file already present"
    fi
    if [[ ! -f "$MODEL_DIR/mmproj-Qwen3.5-9B-F16.gguf" ]]; then
      say "downloading vision projector (mmproj)"
      curl -fL --progress-bar -o "$MODEL_DIR/mmproj-Qwen3.5-9B-F16.gguf.partial" "$MMPROJ_URL"
      mv "$MODEL_DIR/mmproj-Qwen3.5-9B-F16.gguf.partial" "$MODEL_DIR/mmproj-Qwen3.5-9B-F16.gguf"
    else
      skip "mmproj already present"
    fi
    ok "model files in $MODEL_DIR"

    LLAMA_UNIT="/etc/systemd/system/yorik-llamacpp.service"
    sudo tee "$LLAMA_UNIT" >/dev/null <<EOF
[Unit]
Description=Yorik LLM — llama.cpp server (Qwen3.5 9B + vision)
After=docker.service
Requires=docker.service

[Service]
Restart=always
ExecStartPre=-/usr/bin/docker rm -f yorik-llamacpp
ExecStart=/usr/bin/docker run --rm --name yorik-llamacpp \\
  --gpus all -p 127.0.0.1:8080:8080 \\
  -v $MODEL_DIR:/models \\
  ghcr.io/ggml-org/llama.cpp:server-cuda \\
  -m /models/Qwen3.5-9B-Q5_K_M.gguf \\
  --mmproj /models/mmproj-Qwen3.5-9B-F16.gguf \\
  --alias qwen3.5-9b \\
  --host 0.0.0.0 --port 8080 \\
  --ctx-size 65536 --n-gpu-layers -1 --parallel 1 \\
  --jinja -fa on \\
  --cache-type-k q4_0 --cache-type-v q4_0 \\
  --chat-template-kwargs '{"enable_thinking": false}' \\
  --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now yorik-llamacpp >/dev/null 2>&1
    info "waiting for llama.cpp on :8080 (up to 60s)"
    for i in {1..60}; do
      if curl -fsS --max-time 1 http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    if ! curl -fsS http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
      fatal "llama.cpp didn't come up in 60s" \
            "check: sudo journalctl -u yorik-llamacpp"
    fi
    LLM_BASE_URL="http://127.0.0.1:8080/v1"
    LLM_MODEL="qwen3.5-9b"
    ok "llama.cpp serving on ${LLM_BASE_URL}"
    ;;

  ollama)
    if ss -lnt 'sport = :11434' | grep -q LISTEN \
       && ! curl -fsS --max-time 1 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
      fatal "port 11434 is in use by something other than Ollama" \
            "stop it, or pass --llm=existing once you have your LLM started"
    fi
    if ! command -v ollama >/dev/null 2>&1; then
      say "installing Ollama via official installer"
      curl -fsSL https://ollama.com/install.sh | sh >/dev/null
    else
      skip "Ollama already installed"
    fi
    if ! systemctl is-active --quiet ollama 2>/dev/null; then
      sudo systemctl enable --now ollama >/dev/null 2>&1 || \
        nohup ollama serve >/tmp/ollama.log 2>&1 &
      sleep 2
    fi
    info "waiting for Ollama on :11434"
    for i in {1..30}; do
      if curl -fsS --max-time 1 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    if ! curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
      fatal "Ollama didn't come up" "check: systemctl status ollama"
    fi
    ok "Ollama running"

    OLLAMA_MODEL="robit/qwen3.5-9b-r7-research-vision:q4km"
    if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$OLLAMA_MODEL"; then
      skip "$OLLAMA_MODEL already pulled"
    else
      say "pulling $OLLAMA_MODEL (~6.3 GB) — this is the long step"
      ollama pull "$OLLAMA_MODEL"
      ok "model pulled"
    fi
    LLM_BASE_URL="http://127.0.0.1:11434/v1"
    LLM_MODEL="$OLLAMA_MODEL"
    ;;

  none)
    warn "skipping LLM install — Yorik will start but chat won't work until you configure one"
    ;;
esac

# ─── Yorik source + config.env ────────────────────────────────────────
phase "Yorik source"

if (( ! IN_CLONE )); then
  if [[ -d "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    fatal "$INSTALL_DIR exists and is not empty" \
          "pass --dir=<empty path> or remove it"
  fi
  mkdir -p "$INSTALL_DIR"
  git clone --quiet --depth 1 https://github.com/winidi/yorik-ai "$INSTALL_DIR"
  ok "cloned into $INSTALL_DIR"
else
  ok "using existing clone at $INSTALL_DIR"
fi

cd "$INSTALL_DIR"

if [[ ! -f config.env ]]; then
  cp config.env.example config.env
fi
if [[ -n "$LLM_BASE_URL" ]]; then
  sed -i.bak "s|^HOMEOS_LLM_BASE_URL=.*|HOMEOS_LLM_BASE_URL=${LLM_BASE_URL}|" config.env
  sed -i.bak "s|^HOMEOS_MODEL=.*|HOMEOS_MODEL=${LLM_MODEL}|" config.env
  rm -f config.env.bak
  ok "config.env points at ${LLM_BASE_URL}"
fi

# Make sure YORIK_IMMICH_GPU matches the actual host. Without this a
# config.env copied from a CUDA-capable box drags in the 10 GB
# immich-machine-learning:release-cuda image on a CPU-only target,
# blowing the disk budget for no benefit.
if has_nvidia_gpu; then
  IMMICH_GPU_VAL="nvidia"
else
  IMMICH_GPU_VAL="cpu"
fi
if grep -q "^YORIK_IMMICH_GPU=" config.env; then
  sed -i.bak "s|^YORIK_IMMICH_GPU=.*|YORIK_IMMICH_GPU=${IMMICH_GPU_VAL}|" config.env
  rm -f config.env.bak
else
  printf "\nYORIK_IMMICH_GPU=%s\n" "$IMMICH_GPU_VAL" >> config.env
fi
ok "Immich ML variant: ${IMMICH_GPU_VAL}"

# ─── start Yorik via start.sh ─────────────────────────────────────────
phase "Start Yorik (start.sh)"

# start.sh handles: Python venv, voice models, Supabase stack on
# :5435/:8400, FastAPI on :8000. Idempotent — safe to re-run.
#
# YORIK_ALLOW_STALE_DIST=1 — install.sh runs against the bundled
# frontend-react/dist/, which is canonical from the repo. start.sh's
# fingerprint check is for dev (catch the 'edited src/ but didn't
# npm run build' mistake) and shouldn't gate a clean install.
#
# stdin < /dev/null — start.sh has its own interactive systemd
# prompt (line 1078) that would otherwise hang install.sh. The check
# at line 1052 (`[[ -t 0 ]]`) auto-skips it when stdin isn't a TTY.
# Our own systemd handling runs in the next phase below.
YORIK_ALLOW_STALE_DIST=1 bash start.sh </dev/null

# ─── wait for health ──────────────────────────────────────────────────
phase "Wait for ready"

info "waiting for /api/health (up to 120s)"
for i in {1..120}; do
  if curl -fsS --max-time 1 http://localhost:8000/api/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! curl -fsS http://localhost:8000/api/health >/dev/null 2>&1; then
  fatal "Yorik didn't become healthy in 120s" \
        "check: sudo journalctl -u yorik -n 100"
fi
ok "Yorik is healthy"

# Optional cold-install smoke; non-fatal. Skipped on Postgres-backend
# installs because the smoke is specifically the SQLite happy-path
# (against an isolated tmp SQLite DB) — Postgres installs are verified
# by the live /api/health probe above + by the actual app start. The
# smoke against SQLite when the operator is on Postgres surfaces
# legacy-path bugs the operator will never hit, scaring them for
# nothing.
SMOKE_BACKEND=$(grep -E "^YORIK_DB_BACKEND=" config.env 2>/dev/null \
                | head -1 | cut -d= -f2- | tr -d '[:space:]' || echo postgres)
if [[ "$SMOKE_BACKEND" != "postgres" ]] && [[ -x scripts/cold-install-check.sh ]]; then
  info "running cold-install smoke (non-fatal)"
  if ! bash scripts/cold-install-check.sh >/tmp/yorik-coldcheck.log 2>&1; then
    warn "smoke checks failed — see /tmp/yorik-coldcheck.log (Yorik is still running)"
  else
    ok "smoke checks passed"
  fi
elif [[ "$SMOKE_BACKEND" == "postgres" ]]; then
  info "skipping cold-install smoke (Postgres backend — sqlite smoke not applicable)"
fi

# ─── optional systemd autostart ───────────────────────────────────────
phase "Autostart at boot (systemd)"

# Hand off the manually-launched start.sh to systemd so the operator
# sees `systemctl status yorik = active` right away (without that, the
# unit shows inactive until next reboot or a manual `systemctl restart
# yorik`, which is confusing on a fresh install). Stop the manual
# process by its tracked PID, then `systemctl start` re-launches under
# systemd's supervision.
_handoff_to_systemd() {
  local pidfile="/tmp/homeos-api.pid"
  if [[ -r "$pidfile" ]]; then
    local pid
    pid=$(cat "$pidfile" 2>/dev/null || echo "")
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
      # Wait up to 12s for graceful shutdown.
      for _ in {1..24}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
      done
    fi
  fi
  # Also kill any orphan start.sh / uvicorn the PID file might have
  # missed (e.g. if --reload was used or PID wasn't written cleanly).
  pkill -TERM -f "uvicorn backend.main:app.*--port 8000" 2>/dev/null || true
  pkill -TERM -f "bash.*start.sh" 2>/dev/null || true
  sleep 2
  sudo systemctl start yorik.service
  # Wait until /api/health responds again (up to 60s) so the operator's
  # next curl doesn't race the new uvicorn coming up.
  for _ in {1..60}; do
    curl -fsS --max-time 1 http://localhost:8000/api/health >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# Install the parameterised systemd template for tenant Yoriks. The
# host's create-tenant.sh path expects /etc/systemd/system/yorik-tenant@.service
# to exist with paths substituted to match THIS install; without this,
# `systemctl enable --now yorik-tenant@<name>` fails with "Failed to
# load environment files: No such file or directory" because the
# checked-in template ships with placeholder paths.
_install_tenant_template() {
  local dir="$1" user="$2"
  local tenant_template="$dir/infra/systemd/yorik-tenant@.service"
  local tenant_target="/etc/systemd/system/yorik-tenant@.service"
  if [[ ! -f "$tenant_template" ]]; then
    return 0
  fi
  if [[ -f "$tenant_target" ]] \
     && grep -q "$dir" "$tenant_target" 2>/dev/null \
     && grep -q "User=$user" "$tenant_target" 2>/dev/null; then
    return 0  # already installed for this layout
  fi
  local tmp
  tmp=$(mktemp --suffix=.service)
  sed -e "s|{{INSTALL_DIR}}|$dir|g" \
      -e "s|{{INSTALL_USER}}|$user|g" \
      "$tenant_template" > "$tmp"
  sudo install -m 0644 "$tmp" "$tenant_target"
  rm -f "$tmp"
  sudo systemctl daemon-reload
}

# Install the polkit rule that lets the yorik service user manage
# yorik-tenant@*.service units without sudo. yorik.service runs with
# NoNewPrivileges=true (security hardening) which blocks setuid → sudo
# is unusable from inside the service. systemctl talks to systemd
# over D-Bus and polkit authorises; the rule narrows authorization to
# yorik-tenant@* units + caddy reload only.
_install_polkit_rule() {
  local dir="$1" user="$2"
  local rule_src="$dir/infra/polkit/50-yorik-tenant.rules"
  local rule_target="/etc/polkit-1/rules.d/50-yorik-tenant.rules"
  if [[ ! -f "$rule_src" ]]; then
    return 0
  fi
  if [[ ! -d "/etc/polkit-1/rules.d" ]]; then
    # Polkit not installed (very minimal box). Skip silently — the
    # drop flow surfaces a clear error pointing at this file path.
    return 0
  fi
  if [[ -f "$rule_target" ]] \
     && grep -q "YORIK_USER = \"$user\"" "$rule_target" 2>/dev/null; then
    return 0  # already installed for this user
  fi
  local tmp
  tmp=$(mktemp --suffix=.rules)
  sed -e "s|{{INSTALL_USER}}|$user|g" "$rule_src" > "$tmp"
  sudo install -m 0644 "$tmp" "$rule_target"
  rm -f "$tmp"
  # polkit auto-reloads rules.d/ on file change; no daemon-reload needed.
}

SYSTEMD_UNIT="/etc/systemd/system/yorik.service"
if [[ -f "$SYSTEMD_UNIT" ]]; then
  skip "yorik.service already installed at $SYSTEMD_UNIT"
  # Catch up the tenant template if a previous install ran before it
  # was shipped (idempotent — short-circuits when already installed
  # for this layout).
  _install_tenant_template "$INSTALL_DIR" "$INSTALL_USER"
  _install_polkit_rule "$INSTALL_DIR" "$INSTALL_USER"
elif [[ "$FLAG_YES" == "1" ]]; then
  TEMPLATE="$INSTALL_DIR/yorik.service.template"
  if [[ -f "$TEMPLATE" ]]; then
    TMP_UNIT=$(mktemp --suffix=.service)
    sed -e "s|{{INSTALL_DIR}}|$INSTALL_DIR|g" \
        -e "s|{{INSTALL_USER}}|$INSTALL_USER|g" \
        "$TEMPLATE" > "$TMP_UNIT"
    sudo install -m 0644 "$TMP_UNIT" "$SYSTEMD_UNIT"
    rm -f "$TMP_UNIT"
    sudo systemctl daemon-reload
    sudo systemctl enable yorik >/dev/null 2>&1
    _install_tenant_template "$INSTALL_DIR" "$INSTALL_USER"
  _install_polkit_rule "$INSTALL_DIR" "$INSTALL_USER"
    ok "systemd unit installed + enabled (--yes mode)"
    if _handoff_to_systemd; then
      ok "manual launch handed off to systemd (systemctl status yorik → active)"
    else
      warn "handoff to systemd didn't complete in 60s — check journalctl -u yorik"
    fi
  else
    warn "yorik.service.template missing — skipping autostart"
  fi
else
  echo
  echo "  Yorik is currently running from start.sh's manual launch."
  echo "  Should it auto-start when the machine boots? Recommended."
  read -rp "  Install systemd autostart? [Y/n] " CHOICE
  CHOICE="${CHOICE:-Y}"
  if [[ "$CHOICE" =~ ^[Yy]$ ]]; then
    TEMPLATE="$INSTALL_DIR/yorik.service.template"
    if [[ -f "$TEMPLATE" ]]; then
      TMP_UNIT=$(mktemp --suffix=.service)
      sed -e "s|{{INSTALL_DIR}}|$INSTALL_DIR|g" \
          -e "s|{{INSTALL_USER}}|$INSTALL_USER|g" \
          "$TEMPLATE" > "$TMP_UNIT"
      sudo install -m 0644 "$TMP_UNIT" "$SYSTEMD_UNIT"
      rm -f "$TMP_UNIT"
      sudo systemctl daemon-reload
      sudo systemctl enable yorik >/dev/null 2>&1
      _install_tenant_template "$INSTALL_DIR" "$INSTALL_USER"
  _install_polkit_rule "$INSTALL_DIR" "$INSTALL_USER"
      ok "systemd unit installed + enabled"
      if _handoff_to_systemd; then
        ok "manual launch handed off to systemd"
      else
        warn "handoff to systemd didn't complete in 60s — check journalctl -u yorik"
      fi
    else
      warn "yorik.service.template missing — skipping"
    fi
  else
    skip "autostart skipped (re-run install.sh later to add it)"
  fi
fi

# ─── security checklist ───────────────────────────────────────────────
phase "Security checklist"

cat <<EOF

  Yorik holds your real personal data — email, WhatsApp, documents.
  A few OS-level things Yorik can't do for you:

  1. ★ Disk encryption
     If someone walks off with this machine while it's powered OFF,
     can they read $INSTALL_DIR/data/ ? Right now: yes, unless this
     disk has LUKS encryption. The Ubuntu installer offers "Encrypt
     the new Ubuntu installation" — for a fresh box, reinstalling
     with that option is the cleanest path.

  2. Screen lock when you walk away
     GNOME: Settings → Privacy → Screen Lock → 5 min, lock immediately

  3. SSH lockdown (skip if SSH is disabled)
     /etc/ssh/sshd_config:
        PasswordAuthentication no
        PermitRootLogin       no
        PubkeyAuthentication  yes
     Then: sudo systemctl reload ssh

  4. ★ Configure Yorik backups in Settings → Backup
     External SSD + passphrase. Save the passphrase OFF this machine.
     Recovery without it: not possible. That's the whole point.

EOF

# ─── done ─────────────────────────────────────────────────────────────
phase "Done"

cat <<EOF

${BOLD}${GRN}Yorik is up.${RST}

  ${BOLD}Open${RST}    http://localhost:8000

EOF
case "$DECIDED_LLM" in
  ollama)
    cat <<EOF
  ${BOLD}LLM${RST}     Ollama on :11434 — ${OLLAMA_MODEL:-?}
          R7 fine-tune: vision works, thinking auto-disabled
          by Yorik via reasoning_effort=none.

EOF
    ;;
  cuda)
    cat <<EOF
  ${BOLD}LLM${RST}     llama.cpp on :8080 — Qwen3.5 9B Q5_K_M + vision
          systemd unit: yorik-llamacpp.service

EOF
    ;;
  existing)
    cat <<EOF
  ${BOLD}LLM${RST}     ${LLM_BASE_URL} (already running)

EOF
    ;;
esac
cat <<EOF
  ${BOLD}Logs${RST}    sudo journalctl -u yorik -f
  ${BOLD}Stop${RST}    sudo systemctl stop yorik
  ${BOLD}Docs${RST}    ${INSTALL_DIR}/docs/INSTALL.md

  ${DIM}First time: the browser will walk you through admin setup,
  then your home screen.${RST}

EOF
