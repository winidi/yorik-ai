#!/usr/bin/env bash
# HomeOS / Yorik — phased start.
# Idempotent: every phase short-circuits if its work is already done.
# Used at runtime (`./start.sh`) and from install.sh for the first-time setup.

set -euo pipefail
cd "$(dirname "$0")"

# Parse start.sh flags. Keep this tiny and stable — anything richer
# belongs in the `yorik` CLI, not here.
for arg in "$@"; do
  case "$arg" in
    --with-demo)
      # Opt into seeding demo events / tasks / bills on first boot.
      # Default is empty so fresh testers see real empty-state UX.
      # Backend reads this env var inside backend/database.py:seed().
      export YORIK_SEED_DEMO=1
      ;;
    --help|-h)
      echo "Usage: bash start.sh [--with-demo]"
      echo "  --with-demo   Populate demo events/tasks/bills on first boot (default: skip)."
      exit 0
      ;;
  esac
done

CONFIG_FILE="config.env"
if [[ ! -f "$CONFIG_FILE" ]] && [[ -f "config.env.example" ]]; then
  echo "[init] no config.env yet — copying from config.env.example"
  cp config.env.example "$CONFIG_FILE"
fi
if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$CONFIG_FILE"; set +a
else
  echo "[fatal] $CONFIG_FILE missing and config.env.example not present — re-clone the repo."
  exit 1
fi

LLM_BASE_URL="${HOMEOS_LLM_BASE_URL:-http://127.0.0.1:8080/v1}"
LLM_MODEL="${HOMEOS_MODEL:-}"
PORT="${HOMEOS_PORT:-8000}"

# ── Make the `yorik` CLI available on PATH ──────────────────────────
# Harmless per-user symlink in ~/.local/bin so subsequent shells can
# just run `yorik upgrade`, `yorik backup-verify`, etc. without
# typing the relative path. Idempotent: skips if the link already
# points at our scripts/yorik.
_yorik_target="$(pwd)/scripts/yorik"
_yorik_link="$HOME/.local/bin/yorik"
if [[ -x "$_yorik_target" ]]; then
  mkdir -p "$HOME/.local/bin"
  if [[ -L "$_yorik_link" && "$(readlink -f "$_yorik_link")" == "$_yorik_target" ]]; then
    :  # already linked correctly — silent
  elif [[ -L "$_yorik_link" ]]; then
    # Stale symlink from a sibling/previous install (or broken target).
    # Re-point it at THIS install — the running start.sh is by definition
    # the active one. Without -fn, ln refuses and `set -e` aborts the
    # whole script (the bug this fixes).
    ln -sfn "$_yorik_target" "$_yorik_link"
    echo "[init] re-pointed yorik CLI → $_yorik_link (was pointing elsewhere)"
  elif [[ -e "$_yorik_link" ]]; then
    # An actual regular file is in the way — refuse to clobber.
    echo "[init] note: $_yorik_link exists as a real file (not symlink); leaving alone."
    echo "       remove it + re-run start.sh to enable the 'yorik' command."
  else
    ln -s "$_yorik_target" "$_yorik_link"
    echo "[init] linked yorik CLI → $_yorik_link"
    # Most modern distros put ~/.local/bin on PATH automatically (via
    # ~/.profile or systemd-user). If THIS shell doesn't have it,
    # nudge the user instead of silently failing.
    if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
      echo "[init] note: ~/.local/bin isn't on your PATH in this shell yet."
      echo "       Quick fix: add  export PATH=\"\$HOME/.local/bin:\$PATH\""
      echo "       to your ~/.bashrc or ~/.zshrc, then open a new terminal."
    fi
  fi
fi
unset _yorik_target _yorik_link

# Locale: auto-detect system TZ once (used by docker-compose for
# Immich / Paperless display + log timestamps). Onboarding lets the user
# pick a different one later. UTC is the safe fallback.
if [[ -z "${YORIK_TZ:-}" ]]; then
  DETECTED_TZ=""
  if [[ -r /etc/timezone ]]; then
    DETECTED_TZ=$(tr -d '[:space:]' < /etc/timezone)
  elif command -v timedatectl >/dev/null 2>&1; then
    DETECTED_TZ=$(timedatectl show -p Timezone --value 2>/dev/null)
  elif [[ -L /etc/localtime ]]; then
    DETECTED_TZ=$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')
  fi
  YORIK_TZ="${DETECTED_TZ:-UTC}"
  export YORIK_TZ
  # Persist on first run so docker-compose interpolation is stable.
  if [[ -f "$CONFIG_FILE" ]] && ! grep -qE "^YORIK_TZ=" "$CONFIG_FILE"; then
    echo "YORIK_TZ=$YORIK_TZ" >> "$CONFIG_FILE"
  fi
fi

say()  { printf "\033[36m[%s]\033[0m %s\n" "$1" "$2"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
skip() { printf "  \033[33m·\033[0m %s (already installed, skipping)\n" "$1"; }
warn() { printf "  \033[33m![\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; exit 1; }

# Refuse to boot if any of the placeholder secrets from docker-compose.yml
# or config.env survived into the live env. These strings would mean the
# user is running Paperless / Immich with publicly-known credentials.
# The .env file is auto-populated with `openssl rand` further down in this
# script, so under normal flow this check passes — it bites only when a
# stale .env from before the security audit has been carried over.
check_no_default_secrets() {
  local hits=()
  # known placeholder values from compose defaults
  local patterns=("change_me" "please_replace_me")
  for f in .env config.env; do
    [[ -f "$f" ]] || continue
    for pat in "${patterns[@]}"; do
      if grep -q -- "$pat" "$f"; then
        hits+=("$f contains '$pat' — refusing to boot with placeholder secret")
      fi
    done
  done
  # The leaked n8n JWT (commit 73ec099 in git history, from when Yorik
  # bundled n8n). Treat as compromised — kept as a safety check for
  # anyone who copy-pasted from an old config.env into their own n8n.
  local leaked_jti="0a08e5c5-0d4f-419b-8162-9ebb97fd6c6d"
  for f in .env config.env; do
    [[ -f "$f" ]] || continue
    if grep -q -- "$leaked_jti" "$f"; then
      hits+=("$f still contains the n8n JWT leaked in git history (jti $leaked_jti) — if you BYO n8n, rotate via n8n → Settings → API and replace HOMEOS_N8N_API_KEY")
    fi
  done
  if (( ${#hits[@]} > 0 )); then
    echo
    printf "\033[31m✗ refusing to start — default/leaked secrets detected:\033[0m\n"
    for h in "${hits[@]}"; do
      printf "  • %s\n" "$h"
    done
    echo
    echo "Fix:"
    echo "  - For DB/secret-key placeholders: delete the offending line in .env,"
    echo "    then re-run ./start.sh. The script auto-generates fresh values via openssl rand."
    echo "  - For the n8n key (BYO-n8n users only): open your n8n at Settings → API,"
    echo "    revoke the old key, create a new one, paste into config.env."
    exit 1
  fi
}

# ─────────────────────────────────────────────────────────────────────
# PHASE 1 — System packages
# ─────────────────────────────────────────────────────────────────────
say "PHASE 1" "system packages"

phase1_pkgs=(python3 python3-pip ffmpeg curl git)
missing_apt=()
for pkg in "${phase1_pkgs[@]}"; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    skip "$pkg"
  else
    missing_apt+=("$pkg")
  fi
done

# python3-venv ships under different package names per Python minor version
# (python3-venv, python3.10-venv, python3.12-venv, …). The reliable check is
# whether `python3 -m venv --help` actually works.
if python3 -m venv --help >/dev/null 2>&1; then
  skip "python venv module"
else
  # Best-effort: install the version-specific package for this python minor.
  PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  missing_apt+=("python${PYVER}-venv")
fi

if (( ${#missing_apt[@]} > 0 )); then
  if [[ $EUID -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    fail "missing apt packages (${missing_apt[*]}) but neither root nor sudo"
  fi
  say "PHASE 1" "installing: ${missing_apt[*]}"
  SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq "${missing_apt[@]}"
  for pkg in "${missing_apt[@]}"; do ok "installed $pkg"; done
fi

# ─────────────────────────────────────────────────────────────────────
# PHASE 2 — LLM endpoint (bring your own)
# Yorik is a CLIENT to any OpenAI-compatible local LLM. We don't bundle
# an LLM here because installs vary wildly (CPU vs GPU, distro quirks,
# model size) and were the #1 source of first-run failures. Configure
# the endpoint from the UI at: Settings → LLM. The "Detect" button
# scans common ports (Ollama 11434, LM Studio 1234, llama.cpp 8082).
# ─────────────────────────────────────────────────────────────────────
say "PHASE 2" "LLM endpoint (bring your own — configure at Settings → LLM)"

if curl -fs --max-time 2 "$LLM_BASE_URL/models" >/dev/null 2>&1; then
  if [[ -n "$LLM_MODEL" ]]; then
    ok "LLM reachable at $LLM_BASE_URL (model: $LLM_MODEL)"
  else
    ok "LLM reachable at $LLM_BASE_URL (model: pick one in Settings → LLM)"
  fi
else
  warn ""
  warn "================================================================"
  warn "  NO LLM REACHABLE at $LLM_BASE_URL"
  warn "  Yorik's chat will NOT work until you run a local LLM."
  warn ""
  warn "  Yorik is a CLIENT — bring your own OpenAI-compatible backend."
  warn "  Pick whichever fits your hardware + workflow:"
  warn ""
  warn "    Backend           Port    Notes"
  warn "    ─────────────────────────────────────────────────────────"
  warn "    llama-swap        :8080   multi-model, dynamic switching"
  warn "    Ollama            :11434  single-model, easiest install"
  warn "    LM Studio         :1234   GUI, good for desktop tinkering"
  warn "    llama.cpp server  :8081   single model, lowest deps"
  warn "    vLLM              :8000   GPU-heavy production-grade"
  warn ""
  warn "  Whichever you pick, edit config.env:"
  warn "      HOMEOS_LLM_BASE_URL=http://127.0.0.1:<port>/v1"
  warn "      HOMEOS_MODEL=<your-model-name>"
  warn ""
  warn "  …then re-run bash start.sh. Or configure live via"
  warn "  Settings → LLM after boot (has a Detect button)."
  warn "================================================================"
fi

# ─────────────────────────────────────────────────────────────────────
# PHASE 3 — Python environment
# ─────────────────────────────────────────────────────────────────────
say "PHASE 3" "Python environment"

if [[ -d venv ]] && [[ -f venv/bin/activate ]]; then
  skip "venv/ exists"
else
  python3 -m venv venv 2>/dev/null || true
fi

# Debian/Ubuntu trap: the base python3 ships with a venv stub that
# passes `python3 -m venv --help` (so the Phase 1 check earlier
# is satisfied) but ACTUAL venv creation fails inside ensurepip
# unless the matching python3.X-venv package is installed. The
# failure half-creates venv/ — directory exists, bin/activate
# doesn't. Detect that and auto-install the right package.
if [[ ! -f venv/bin/activate ]]; then
  PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  warn "venv creation didn't produce bin/activate — installing python${PYVER}-venv + retrying"
  if [[ $EUID -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
    fail "need root or sudo to install python${PYVER}-venv (apt)"
  fi
  SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq "python${PYVER}-venv"
  rm -rf venv
  python3 -m venv venv
  [[ -f venv/bin/activate ]] || fail "venv still broken after installing python${PYVER}-venv — check 'python3 -m venv venv' manually"
  ok "venv re-created with python${PYVER}-venv"
fi
ok "venv/ ready"
# shellcheck disable=SC1091
source venv/bin/activate

# Hash the requirements file so we only re-install when it changed.
REQ_HASH_FILE="venv/.requirements.sha256"
NEW_HASH=$(sha256sum backend/requirements.txt | awk '{print $1}')
if [[ -f "$REQ_HASH_FILE" ]] && [[ "$(cat "$REQ_HASH_FILE")" == "$NEW_HASH" ]]; then
  skip "pip install -r backend/requirements.txt (hash unchanged)"
else
  say "PHASE 3" "pip install -r backend/requirements.txt"
  pip install --quiet --upgrade pip
  pip install --quiet -r backend/requirements.txt
  echo "$NEW_HASH" > "$REQ_HASH_FILE"
  ok "python deps installed"
fi

# ─────────────────────────────────────────────────────────────────────
# PHASE 4 — Model files (Whisper, Supertonic TTS, SpeechBrain ECAPA)
# ─────────────────────────────────────────────────────────────────────
say "PHASE 4" "model files"

# Pre-flight: data/ must be writable by the host user before we mkdir
# anything inside it. dockerd auto-creates bind-mount source paths as
# root when missing — if a previous compose run did that, data/ is
# root-owned and `mkdir -p data/voices` would fail with "Permission
# denied" partway through PHASE 4. Fail fast with the exact chown
# command instead of dying mid-download.
if [[ -e data ]] && [[ ! -w data ]]; then
  echo
  echo "──────────────── data/ is not writable by you ─────────────"
  ls -la data 2>/dev/null | head -3 | sed 's/^/    /'
  echo
  echo "  data/ exists but is owned by another user (usually root,"
  echo "  from a previous 'docker compose up' that created the bind-"
  echo "  mount source paths via dockerd). Restore ownership with:"
  echo
  echo "    sudo chown \$(id -u):\$(id -g) data"
  echo
  echo "  Then re-run this script. NOTE: this is non-recursive on"
  echo "  purpose — postgres subdirs (data/immich/postgres, data/"
  echo "  paperless/db) must keep their container UID 999 ownership."
  echo "─────────────────────────────────────────────────────────"
  exit 1
fi

mkdir -p data "${HOMEOS_VOICES_DIR:-data/voices}"

# Whisper — downloads on first import; cache lives at ~/.cache/whisper
WHISPER_NAME="${HOMEOS_WHISPER_MODEL:-base}"
if python3 -c "import os, whisper, sys; p = os.path.join(os.path.expanduser('~'), '.cache/whisper', '${WHISPER_NAME}.pt'); sys.exit(0 if os.path.exists(p) else 1)" 2>/dev/null; then
  skip "whisper $WHISPER_NAME already cached"
else
  say "PHASE 4" "downloading whisper $WHISPER_NAME"
  python3 -c "import whisper; whisper.load_model('${WHISPER_NAME}')"
  ok "whisper $WHISPER_NAME downloaded"
fi

# Supertonic 3 — single ONNX model handles 31 languages including German.
# Pre-fetches the model files (~few hundred MB) from HuggingFace so the
# first /api/ask-voice request isn't slow. Cached at
# ${HOMEOS_VOICES_DIR:-data/voices}/supertonic-3/ — subsequent runs are
# fully offline.
SUPERTONIC_DIR="${HOMEOS_VOICES_DIR:-data/voices}/supertonic-3"
# Supertonic puts ONNX weights under $SUPERTONIC_DIR/onnx/, not the
# top level. A top-level glob misses them and re-triggers the download
# on every start.sh run — use `find` so any layout (today or future)
# counts as "model present".
if [[ -d "$SUPERTONIC_DIR" ]] && [[ -n "$(find "$SUPERTONIC_DIR" -name '*.onnx' -print -quit 2>/dev/null)" ]]; then
  skip "supertonic-3 model present at $SUPERTONIC_DIR"
else
  say "PHASE 4" "downloading supertonic-3 model (one-time, ~few hundred MB)"
  # huggingface_hub's tqdm progress bars use carriage-return-based
  # in-place updates that look fine in a TTY but leave a trail of
  # escape sequences when the install transcript is piped to a log
  # file (the usual `bash install.sh | tee install.log` case). Disable
  # them — the say()/ok() lines around this are enough operator signal.
  HF_HUB_DISABLE_PROGRESS_BARS=1 \
  python3 -c "from backend.tts import warm_up; warm_up()" \
    || warn "supertonic-3 download failed — retry by re-running start.sh"
  ls "$SUPERTONIC_DIR"/*.onnx >/dev/null 2>&1 && ok "supertonic-3 ready"
fi

# Pre-synthesize the voice acknowledgement phrases ("klar, moment", "on it"…)
# for every supported language. Played by the voice FAB the moment STT
# finishes so users don't wait in silence while the LLM cooks. Results
# are cached to disk under ${HOMEOS_VOICES_DIR}/acks/ so this step is
# slow only on the very first boot (~15-30s for the ONNX cold-load +
# synthesis pass); subsequent boots load from disk in <200ms.
# stderr still suppressed because the JSON logger spams structured log
# lines to it — the warmup prints its own human-readable [acks] progress
# to stdout so users see what's happening.
python3 -c "from backend import voice_acks; voice_acks.warmup()" 2>/dev/null \
  && ok "voice acknowledgements ready" \
  || warn "voice ack warmup failed — voice will still work, just no instant feedback"

# Speaker identification — SpeechBrain ECAPA-TDNN. Replaces Resemblyzer
# because ECAPA holds up at 1-2 second utterances (real voice commands).
# Model is stored INSIDE the project at HOMEOS_SPEAKER_MODEL_DIR (default
# data/speaker_model) so the shipped box doesn't depend on ~/.cache survival.
SPK_DIR="${HOMEOS_SPEAKER_MODEL_DIR:-data/speaker_model}"
if [[ -f "$SPK_DIR/embedding_model.ckpt" ]] || [[ -f "$SPK_DIR/hyperparams.yaml" ]]; then
  skip "speechbrain ECAPA model present at $SPK_DIR"
else
  say "PHASE 4" "downloading SpeechBrain ECAPA model to $SPK_DIR"
  python3 -c "
from speechbrain.inference.speaker import EncoderClassifier
EncoderClassifier.from_hparams(source='speechbrain/spkrec-ecapa-voxceleb', savedir='$SPK_DIR')
" 2>/dev/null && ok "speechbrain ECAPA ready" \
  || warn "speechbrain init failed — voice-profile features will be unavailable"
fi

# NOTE: the embedder is no longer started here. It's served by whichever
# OpenAI-compatible LLM backend the user configured (Settings → LLM).
# Ollama and llama.cpp both expose /v1/embeddings; LM Studio does too.

# ─────────────────────────────────────────────────────────────────────
# PHASE 5 — Database
# ─────────────────────────────────────────────────────────────────────
say "PHASE 5" "database"
# Postgres-backend installs need the bundled Supabase stack up (with
# all Yorik migrations applied) before backend.database can connect.
# The bootstrap script is idempotent — on a workstation that's already
# been brought up by hand, it just records the existing migrations
# and exits in ~3s.
if [[ "${YORIK_DB_BACKEND:-postgres}" == "postgres" ]]; then
  bash scripts/bootstrap-supabase.sh
fi
python3 -m backend.database >/dev/null
ok "${HOMEOS_DB_PATH:-data/family.db} schema + seed verified"
ok "${HOMEOS_DOCS_DB_PATH:-data/documents.db} (RAG corpus + vector index) verified"

# ─────────────────────────────────────────────────────────────────────
# PHASE 6 — Services
# ─────────────────────────────────────────────────────────────────────
say "PHASE 6" "services"

# Stop any old uvicorn we previously started
if [[ -f /tmp/homeos-api.pid ]]; then
  OLD_PID="$(cat /tmp/homeos-api.pid)"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" || true
    sleep 1
    ok "stopped previous uvicorn (pid $OLD_PID)"
  fi
  rm -f /tmp/homeos-api.pid
fi

# Cross-install collision: the pidfile only tracks the LAST start.sh
# from THIS folder. A uvicorn launched by a sibling install (different
# clone path) holds the port invisibly to the check above and our own
# `nohup uvicorn` below crashes with "Errno 98: Address already in use".
# Detect the holder; if it's a Yorik uvicorn (root OR a multiprocessing
# worker forked off uvicorn --reload), take over.
if command -v ss >/dev/null 2>&1; then
  HOLDER_LINE="$(ss -Hltnp "sport = :$PORT" 2>/dev/null | head -1 || true)"
  if [[ -n "$HOLDER_LINE" ]]; then
    HOLDER_PID="$(printf '%s\n' "$HOLDER_LINE" | sed -nE 's/.*pid=([0-9]+).*/\1/p' | head -1)"
    HOLDER_CMD=""
    UVICORN_PID=""
    if [[ -n "$HOLDER_PID" ]]; then
      HOLDER_CMD="$(ps -p "$HOLDER_PID" -o args= 2>/dev/null || true)"
      # Walk up the process tree (max 6 hops) looking for a
      # "uvicorn backend.main" ancestor. uvicorn --reload spawns
      # multiprocessing.spawn workers whose own cmdline contains no
      # "uvicorn" at all — but their parent does.
      WALK_PID="$HOLDER_PID"
      WALK_DEPTH=0
      while [[ -n "$WALK_PID" && "$WALK_PID" != "1" && $WALK_DEPTH -lt 6 ]]; do
        WALK_CMD="$(ps -p "$WALK_PID" -o args= 2>/dev/null || true)"
        if [[ "$WALK_CMD" == *"uvicorn"*"backend.main"* ]]; then
          UVICORN_PID="$WALK_PID"
          break
        fi
        WALK_PID="$(ps -p "$WALK_PID" -o ppid= 2>/dev/null | tr -d ' ' || true)"
        WALK_DEPTH=$((WALK_DEPTH + 1))
      done
      # Orphan fallback: if the parent uvicorn was killed but the
      # multiprocessing worker stayed alive (reparented to PID 1), the
      # walk above won't find an ancestor. Recognise the worker by its
      # cmdline shape — a venv python interpreter running
      # multiprocessing.spawn is unambiguously a stale uvicorn child.
      if [[ -z "$UVICORN_PID" ]] \
         && [[ "$HOLDER_CMD" == *"venv/bin/python"* ]] \
         && [[ "$HOLDER_CMD" == *"multiprocessing.spawn"* ]]; then
        UVICORN_PID="$HOLDER_PID"
        ORPHAN_WORKER=1
      fi
    fi
    if [[ -n "$UVICORN_PID" ]]; then
      if [[ "${ORPHAN_WORKER:-0}" == "1" ]]; then
        warn "port $PORT held by an ORPHANED Yorik uvicorn worker pid $HOLDER_PID (parent already dead)"
      elif [[ "$UVICORN_PID" != "$HOLDER_PID" ]]; then
        warn "port $PORT held by Yorik uvicorn worker pid $HOLDER_PID (parent uvicorn pid $UVICORN_PID)"
      else
        warn "port $PORT held by a Yorik uvicorn (pid $UVICORN_PID)"
      fi
      warn "  stopping it so this install can take over :$PORT"
      # Kill children first so they don't respawn while we kill the parent.
      pkill -TERM -P "$UVICORN_PID" 2>/dev/null || true
      kill "$UVICORN_PID" 2>/dev/null || true
      sleep 2
      if kill -0 "$UVICORN_PID" 2>/dev/null; then
        pkill -KILL -P "$UVICORN_PID" 2>/dev/null || true
        kill -9 "$UVICORN_PID" 2>/dev/null || true
        sleep 1
      fi
      ok "stopped stale uvicorn (pid $UVICORN_PID)"
    elif [[ -n "$HOLDER_PID" ]]; then
      fail "port $PORT held by pid $HOLDER_PID ($HOLDER_CMD) — not a Yorik uvicorn. Set HOMEOS_PORT to a free port or stop that process, then re-run."
    else
      fail "port $PORT is in use but the holder isn't visible (likely owned by another user). Investigate: sudo ss -ltnp 'sport = :$PORT'"
    fi
  fi
fi

# ── frontend dist/src fingerprint check ────────────────────────────
# Refuse to ship a stale UI. dist/ is committed; if the maintainer
# edited src/ without `npm run build` the fingerprint won't match.
# Auto-rebuilds when npm is available (silently fixes the workstation
# forgot-to-build case); falls back to a warn-with-bypass when it
# isn't (alpha testers don't all have Node installed). YORIK_ALLOW_STALE_DIST=1
# skips the check entirely.
say "FRONTEND" "checking dist/ matches src/"
if [[ -d "frontend-react/src" && -f "frontend-react/scripts/fingerprint.sh" ]]; then
  if [[ "${YORIK_ALLOW_STALE_DIST:-}" == "1" ]]; then
    # install.sh sets YORIK_ALLOW_STALE_DIST=1 when invoking start.sh
    # because a fresh clone always has matching dist+src by construction.
    # Operators running start.sh by hand only see this line if THEY set
    # the var deliberately.
    skip "frontend dist/ check skipped (caller set YORIK_ALLOW_STALE_DIST=1)"
  else
    EXPECTED_FP="$(cat frontend-react/dist/.src-fingerprint 2>/dev/null || echo missing)"
    ACTUAL_FP="$(bash frontend-react/scripts/fingerprint.sh)"
    if [[ "$EXPECTED_FP" == "$ACTUAL_FP" ]]; then
      ok "frontend dist/ matches src/ (fingerprint ${ACTUAL_FP:0:8})"
    elif [[ "$EXPECTED_FP" == "missing" ]]; then
      warn "frontend-react/dist/.src-fingerprint missing — first run after this check landed, or pre-fingerprint dist"
      warn "  expected: <none>  actual: ${ACTUAL_FP:0:12}"
    else
      warn "frontend dist/ is STALE — src/ has changed since the last build"
      warn "  expected: ${EXPECTED_FP:0:12}  actual: ${ACTUAL_FP:0:12}"
    fi
    if [[ "$EXPECTED_FP" != "$ACTUAL_FP" ]]; then
      if command -v npm >/dev/null 2>&1 && [[ -d frontend-react/node_modules ]]; then
        say "FRONTEND" "auto-rebuilding (npm run build in frontend-react/)"
        ( cd frontend-react && npm run build ) || fail "frontend build failed — fix above, or set YORIK_ALLOW_STALE_DIST=1 to start with the existing dist/"
        ok "frontend rebuilt"
      else
        warn "  npm or node_modules unavailable — can't auto-rebuild"
        warn "  install Node and run: (cd frontend-react && npm install && npm run build)"
        warn "  OR start with the existing (possibly stale) dist anyway: YORIK_ALLOW_STALE_DIST=1 bash start.sh"
        fail "refusing to start with stale dist/"
      fi
    fi
  fi
fi

LOG=/tmp/homeos-api.log

# Default to all interfaces — most self-hosters want Yorik reachable
# from their phone / other LAN devices without having to remember a
# flag on every start. Login is bcrypt; for anything beyond a trusted
# home LAN, put TLS + a reverse proxy (Caddy) or Tailscale in front.
# To restrict to the host machine only:
#   YORIK_BIND=127.0.0.1 bash start.sh
YORIK_BIND="${YORIK_BIND:-0.0.0.0}"
if [[ "$YORIK_BIND" == "0.0.0.0" || "$YORIK_BIND" == "::" ]]; then
  warn "binding to $YORIK_BIND — Yorik is reachable from anyone on this LAN"
  warn "  use TLS + reverse proxy (Caddy) or Tailscale for anything beyond a trusted home LAN"
  warn "  to bind localhost only: YORIK_BIND=127.0.0.1 bash start.sh"
fi

# --reload is dev convenience — it walks the entire project tree on
# every change check, which crashes (PermissionError) on the
# root-owned data/immich/postgres bind mount the moment Immich is
# bundled. Opt out by default; YORIK_DEV_RELOAD=1 brings it back
# for maintainers actively editing backend/.
RELOAD_ARG=""
if [[ "${YORIK_DEV_RELOAD:-0}" == "1" ]]; then
  RELOAD_ARG="--reload"
fi
nohup uvicorn backend.main:app --host "$YORIK_BIND" --port "$PORT" $RELOAD_ARG >"$LOG" 2>&1 &
echo $! > /tmp/homeos-api.pid
ok "uvicorn started on $YORIK_BIND:$PORT  (pid $(cat /tmp/homeos-api.pid), log: $LOG)"

_docker_ready() {
  # `docker` CLI being on PATH doesn't mean the daemon is responding —
  # `apt install docker.io` (vs the get.docker.com installer) ships
  # the service DISABLED on Ubuntu, and group membership doesn't
  # propagate to existing shells. This auto-recovers from both:
  #   - tries to enable+start docker via systemctl/service if down
  #   - prints a clear remediation if neither works
  # Returns 0 if docker is usable, 1 otherwise.
  command -v docker >/dev/null 2>&1 || return 1
  docker info >/dev/null 2>&1 && return 0
  say "DOCKER" "daemon isn't responding — trying to start it"
  local SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
  if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl enable --now docker 2>/dev/null \
      || $SUDO service docker start 2>/dev/null || true
  else
    $SUDO service docker start 2>/dev/null || true
  fi
  sleep 2
  docker info >/dev/null 2>&1 && return 0
  warn "Docker daemon still unreachable. Two common causes on Ubuntu:"
  warn "  1) your user isn't in the 'docker' group yet:"
  warn "       sudo usermod -aG docker $USER && newgrp docker"
  warn "  2) the daemon is masked or broken — try:"
  warn "       sudo systemctl status docker"
  warn "Skipping Immich + Paperless. Re-run start.sh once Docker is up."
  return 1
}

if _docker_ready; then
  # ── Ensure .env has the per-service secrets the compose file expects.
  #    Generated once with openssl rand and never re-rolled; ignored by git.
  touch .env
  _gen_secret_if_missing() {
    local key="$1" len="${2:-32}"
    if ! grep -qE "^${key}=" .env; then
      echo "${key}=$(openssl rand -hex "$len")" >> .env
      ok "generated ${key} in .env"
    fi
  }
  _gen_secret_if_missing IMMICH_DB_PASSWORD 24
  _gen_secret_if_missing PAPERLESS_DB_PASSWORD 24
  _gen_secret_if_missing PAPERLESS_SECRET_KEY 32
  _gen_secret_if_missing PAPERLESS_YORIK_TOKEN 24
  # Paperless admin defaults: created once on first container start.
  grep -qE "^PAPERLESS_ADMIN_USER="     .env || echo "PAPERLESS_ADMIN_USER=admin" >> .env
  if ! grep -qE "^PAPERLESS_ADMIN_PASSWORD=" .env; then
    PWD_=$(openssl rand -hex 12)
    echo "PAPERLESS_ADMIN_PASSWORD=$PWD_" >> .env
    ok "generated Paperless admin password (in .env as PAPERLESS_ADMIN_PASSWORD)"
  fi
  grep -qE "^PAPERLESS_ADMIN_MAIL="     .env || echo "PAPERLESS_ADMIN_MAIL=admin@yorik.local" >> .env

  # Last line of defense: refuse to start the docker stack with known-bad
  # values (placeholder DB passwords from the compose file, plus a legacy
  # n8n JWT once leaked in git history from when Yorik bundled n8n —
  # kept as a safety net for BYO-n8n users who copy-pasted from an old
  # config.env). On a fresh install all values come from the `openssl
  # rand` block above so this passes silently.
  check_no_default_secrets

  # Bring everything up (idempotent). If an earlier run created
  # data/paperless or data/immich as root (e.g. someone ran start.sh
  # with sudo by mistake, or a container wrote files we can't unlink),
  # the mkdir below will fail with bare "Permission denied" lines and
  # nothing actionable. Catch that case early and surface the same
  # SCOPED chown the ownership pre-flight further down would suggest.
  if ! mkdir -p data/paperless/{db,data,media,export,consume} \
                data/immich/{library,postgres} 2>/dev/null; then
    echo
    echo "──────────────── Can't create data subdirs ───────────────"
    echo "  mkdir failed under data/paperless and/or data/immich."
    echo "  Almost always: those dirs already exist owned by root"
    echo "  (a previous run was started with sudo, or a container"
    echo "  wrote files as root). Your user can't create children"
    echo "  inside a root-owned dir."
    echo
    echo "  If this is a fresh install with no real postgres state"
    echo "  yet, the safe one-shot fix is:"
    echo
    echo "    sudo chown -R \$(id -u):\$(id -g) data/paperless data/immich"
    echo
    echo "  If you already have working bundled services, scope it"
    echo "  tighter — leave data/immich/postgres and data/paperless/db"
    echo "  alone (they need UID 999):"
    echo
    echo "    sudo chown -R \$(id -u):\$(id -g) \\"
    echo "      data/documents \\"
    echo "      data/immich/library \\"
    echo "      data/paperless/data data/paperless/media \\"
    echo "      data/paperless/export data/paperless/consume"
    echo
    echo "  Then re-run start.sh."
    echo "─────────────────────────────────────────────────────────"
    exit 1
  fi

  # ── BYO detection: skip bundled services whose port is already taken.
  # If something already answers on Immich/Paperless/WhatsApp's
  # default port (or n8n's :5678 — which Yorik never bundles but does
  # proxy at /n8n/ if you run it yourself), point Yorik at the existing
  # instance instead of
  # spinning up a duplicate (and crashing on port-bind).
  #
  # /dev/tcp/ is a bash builtin — no nc/curl dependency, no false
  # positives from HTTP-only probes against Postgres-ish services.
  _set_env() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" "$CONFIG_FILE"; then
      sed -i.bak -E "s|^${key}=.*|${key}=${value}|" "$CONFIG_FILE" && rm -f "$CONFIG_FILE.bak"
    else
      echo "${key}=${value}" >> "$CONFIG_FILE"
    fi
  }
  _port_in_use() {
    (echo > /dev/tcp/127.0.0.1/"$1") 2>/dev/null
  }
  _is_our_container() {
    # If WE started it (yorik-* container is running on that port),
    # it doesn't count as a "user's existing instance" — we treat it
    # as ours and re-include the profile so docker compose updates it.
    # Also matches legacy homeos-* containers from pre-rename installs
    # so upgrade paths keep working.
    docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
      | grep -qE "^(yorik|homeos)-$1 .*:$2->"
  }

  PROFILES=()
  # n8n is BYO — Yorik can't bundle it (Sustainable Use License is
  # source-available, not OSI-approved open source; bundling would
  # drag SUL distribution terms into the AGPL-3 codebase). The /n8n/*
  # reverse proxy + connector hooks stay in the backend for users who
  # run their own n8n separately. If something's on :5678 already we
  # assume it's theirs and point Yorik at it.
  if _port_in_use 5678; then
    skip "n8n detected on :5678 — Yorik will proxy at /n8n/ (BYO; see docs/CONNECTORS.md)"
    _set_env "HOMEOS_N8N_BASE_URL" "http://127.0.0.1:5678"
  fi
  for svc_port in "immich:2283:" \
                  "paperless:8010:PAPERLESS_INTERNAL_URL" \
                  "whatsapp:3015:YORIK_WA_BRIDGE_URL"; do
    svc="$(echo "$svc_port" | cut -d: -f1)"
    port="$(echo "$svc_port" | cut -d: -f2)"
    envkey="$(echo "$svc_port" | cut -d: -f3)"

    # Per-service opt-out: YORIK_ENABLE_IMMICH=0 / _PAPERLESS=0 /
    # _WHATSAPP=0 in config.env skip bundling entirely. The Yorik UI
    # for the disabled service stays installed (so the user can flip
    # the flag later and re-run start.sh without reinstalling) but
    # the container never comes up. Useful for: "I don'\''t use
    # WhatsApp", "I have an external Paperless on another box", or
    # "this machine doesn'\''t have the disk for Immich".
    upper="$(echo "$svc" | tr "[:lower:]" "[:upper:]")"
    enable_var="YORIK_ENABLE_${upper}"
    enable_val="${!enable_var:-1}"
    if [[ "$enable_val" == "0" || "$enable_val" == "false" || "$enable_val" == "no" ]]; then
      skip "$svc disabled via $enable_var=$enable_val — not bundling, no container will start"
      continue
    fi

    if _is_our_container "$svc" "$port" || ! _port_in_use "$port"; then
      PROFILES+=("bundled-$svc")
    else
      skip "$svc already on :$port — Yorik will use your existing instance (not bundling)"
      [[ -n "$envkey" ]] && _set_env "$envkey" "http://127.0.0.1:$port"
      [[ "$svc" == "immich" ]] && warn "  ↳ open Settings → Connectors → Immich and paste your Immich URL + admin API key"
      [[ "$svc" == "paperless" ]] && { PAPERLESS_BYO=true; warn "  ↳ open Settings → Connectors → Paperless and paste your Paperless API token"; }
    fi
  done

  if (( ${#PROFILES[@]} == 0 )); then
    ok "all four services already running — nothing to bundle"
  else
    export COMPOSE_PROFILES="$(IFS=,; echo "${PROFILES[*]}")"

    # One-time question: does this machine have an NVIDIA GPU + the
    # container toolkit installed? Answer is persisted in config.env
    # as YORIK_IMMICH_GPU=nvidia or =cpu. If yes, we layer in the
    # NVIDIA override (release-cuda image + GPU passthrough). If no,
    # the default CPU-safe config runs everywhere.
    # The user can flip later by editing config.env.
    COMPOSE_FILES=("-f" "docker-compose.yml")
    if [[ "${PROFILES[*]}" =~ bundled-immich ]]; then
      if [[ -z "${YORIK_IMMICH_GPU:-}" ]]; then
        # Non-TTY install (CI, systemd-unit first-boot, scripted clone-
        # and-run): default to CPU silently so the script never hangs
        # waiting for input. Interactive users still get the prompt.
        if [[ ! -t 0 ]]; then
          YORIK_IMMICH_GPU=cpu
          echo "  → non-interactive install detected — defaulting Immich to CPU"
          echo "    (edit YORIK_IMMICH_GPU=nvidia in config.env to switch)"
        else
          echo
          echo "──────────────── Immich GPU acceleration ─────────────────"
          echo "  Immich does face recognition + smart search via ML."
          echo "  An NVIDIA GPU + nvidia-container-toolkit makes it ~30x"
          echo "  faster than CPU. (Apple Silicon / AMD / no-GPU: pick No.)"
          echo
          echo "  Requires on the host: nvidia-container-toolkit"
          echo "    sudo apt install -y nvidia-container-toolkit"
          echo "    sudo systemctl restart docker"
          echo "─────────────────────────────────────────────────────────"
          read -r -p "Use NVIDIA GPU for Immich? [y/N] " _gpu_yn
          if [[ "${_gpu_yn,,}" == "y" || "${_gpu_yn,,}" == "yes" ]]; then
            YORIK_IMMICH_GPU=nvidia
          else
            YORIK_IMMICH_GPU=cpu
          fi
        fi
        echo "YORIK_IMMICH_GPU=$YORIK_IMMICH_GPU" >> "$CONFIG_FILE"
        export YORIK_IMMICH_GPU
        echo "  → saved YORIK_IMMICH_GPU=$YORIK_IMMICH_GPU to config.env"
      fi
      # Generic backend layering: for any non-cpu value, look for the
      # matching docker-compose.<backend>.yml override (nvidia ships
      # with the repo; openvino/rocm/armnn/rknn are generated on-demand
      # by scripts/immich-ml-backend.sh). Keeps backward-compat with
      # the legacy `nvidia` literal AND supports the broader set
      # without a code change here every time.
      if [[ "$YORIK_IMMICH_GPU" != "cpu" && "$YORIK_IMMICH_GPU" != "" ]]; then
        _override="docker-compose.${YORIK_IMMICH_GPU}.yml"
        if [[ -f "$_override" ]]; then
          COMPOSE_FILES+=("-f" "$_override")
        else
          say "IMMICH" "WARN: YORIK_IMMICH_GPU=$YORIK_IMMICH_GPU but $_override missing — run: bash scripts/immich-ml-backend.sh set $YORIK_IMMICH_GPU"
        fi
      fi
      say "IMMICH" "ML mode: $YORIK_IMMICH_GPU"
    fi

    # Pre-flight: docker-compose runs immich-server + paperless-web as
    # the host user so files written under data/ are owned by the
    # host. Reasons differ slightly per service:
    #   - immich/library: lets "Move to external SSD" relocate the
    #     photo originals later (immich IS in RELOCATABLE).
    #   - paperless/{data,media,export,consume}: just so the host
    #     user can read/back-up/chown them. Paperless stays on the
    #     internal disk regardless (it's NOT in RELOCATABLE — see
    #     backend/storage.py).
    # If a pre-fix install left root-owned files under those dirs the
    # containers crash on next start with cryptic "permission denied"
    # logs. Detect + tell the user the exact SCOPED chown.
    #
    # Scope is important: data/immich/postgres and data/paperless/db
    # are deliberately NOT in this list. Those containers run as their
    # internal `postgres` user (UID 999); chowning them to the host
    # user breaks postgres on next boot. (Real bug we just fixed —
    # a previous version of this script suggested the wholesale chown
    # and made it worse for one user.)
    _ownership_warn=0
    _my_uid=$(id -u)
    _host_owned_dirs=(
      "data/immich/library"
      "data/paperless/data"
      "data/paperless/media"
      "data/paperless/export"
      "data/paperless/consume"
    )
    for _dir in "${_host_owned_dirs[@]}"; do
      if [[ -d "$_dir" ]]; then
        _bad=$(find "$_dir" \( -type d -o -type f \) \
                    ! -user "$_my_uid" -print -quit 2>/dev/null || true)
        if [[ -n "$_bad" ]]; then
          if [[ "$_ownership_warn" == "0" ]]; then
            echo
            echo "──────────────── Ownership mismatch on data/ ─────────────"
          fi
          _ownership_warn=1
          echo "  ! $_dir has files not owned by you (uid=$_my_uid)"
          echo "    First offender: $_bad"
        fi
      fi
    done
    if [[ "$_ownership_warn" == "1" ]]; then
      echo
      echo "  Immich and Paperless need to run as your UID so the"
      echo "  containers can write to their bind-mounted data dirs."
      echo "  (Immich also needs this so 'Move to external SSD' can"
      echo "  relocate the photo library later — Paperless stays on"
      echo "  the internal disk regardless.) Existing root-owned"
      echo "  files (from before this change) must be chowned ONCE"
      echo "  before the containers can start. ONLY chown the host-"
      echo "  owned dirs — leave data/immich/postgres and data/"
      echo "  paperless/db alone (they need UID 999):"
      echo
      echo "    sudo chown -R \$(id -u):\$(id -g) \\"
      echo "      data/immich/library \\"
      echo "      data/paperless/data data/paperless/media \\"
      echo "      data/paperless/export data/paperless/consume"
      echo
      echo "  Then re-run start.sh."
      echo "─────────────────────────────────────────────────────────"
      exit 1
    fi

    if docker compose "${COMPOSE_FILES[@]}" ps --status=running 2>/dev/null | grep -q "yorik-"; then
      skip "docker stack already running (profiles: $COMPOSE_PROFILES)"
    else
      say "DOCKER" "bringing up: $COMPOSE_PROFILES (first time may pull ~5GB)"
      docker compose "${COMPOSE_FILES[@]}" up -d >/dev/null
      ok "docker stack started"
    fi

    # Immich first-boot stale-bind-mount auto-heal. Symptom: container
    # binds /home/$USER/yorik-ai/data/immich/library → /data, but the
    # host dir was recreated (mkdir/chown sequence in this script,
    # or a fresh-install reset) AFTER the compose mount landed. The
    # container then sees /data/library empty, healthcheck calls
    # `statfs /data/library` → ENOENT, uploads ALSO fail with ENOENT,
    # and `provision_immich` from /api/auth/setup can't create the
    # admin user (because the server is unhealthy). Both visible
    # symptoms — "I needed to make an Immich account manually" and
    # "first upload crashed Immich" — resolve when the container
    # re-binds. Polling its healthcheck for up to 3 min covers
    # first-install init time (DB migrations); if it's still
    # unhealthy after that, restart once and re-check.
    if [[ "${PROFILES[*]}" =~ bundled-immich ]]; then
      say "DOCKER" "waiting for Immich to reach healthy (up to 3 min on first install)"
      immich_ok=0
      for _ in $(seq 1 90); do
        status=$(docker inspect --format='{{.State.Health.Status}}' \
                 yorik-immich-server 2>/dev/null || echo unknown)
        if [[ "$status" == "healthy" ]]; then
          immich_ok=1; break
        fi
        sleep 2
      done
      if [[ $immich_ok -eq 1 ]]; then
        ok "Immich healthy"
      else
        warn "Immich didn't reach healthy in 3 min — restarting once to clear any stale bind-mount"
        docker restart yorik-immich-server >/dev/null 2>&1 || true
        # Give the restart 60s to land + re-pass healthcheck.
        for _ in $(seq 1 30); do
          status=$(docker inspect --format='{{.State.Health.Status}}' \
                   yorik-immich-server 2>/dev/null || echo unknown)
          if [[ "$status" == "healthy" ]]; then
            immich_ok=1; break
          fi
          sleep 2
        done
        if [[ $immich_ok -eq 1 ]]; then
          ok "Immich healthy after restart"
        else
          warn "Immich still not healthy — inspect: docker logs --tail 50 yorik-immich-server"
        fi
      fi
    fi

    export COMPOSE_FILE_ARGS="${COMPOSE_FILES[*]}"
  fi

  # Re-source config.env so any HOMEOS_* env vars we just wrote to it
  # are visible to the rest of this script (paperless token fetch etc).
  set -a; source "$CONFIG_FILE"; set +a

  # ── Paperless first-run: wait for it, then grab the API token via the
  #    Django shell and store it in Yorik's app_settings so the connector
  #    just works.  Skipped on subsequent runs if a token is already saved.
  #
  # BYO Paperless: the docker-exec token grab below targets our own
  # yorik-paperless-web container — it's meaningless against a user's
  # external instance. Short-circuit with a clear nudge to Connectors.
  if [[ "${PAPERLESS_BYO:-false}" == "true" ]]; then
    skip "paperless is BYO — paste your API token at Settings → Connectors → Paperless"
  elif ! python3 -c "
from backend.database import conn_ctx, DEFAULT_DB_PATH
import os
with conn_ctx(os.getenv('HOMEOS_DB_PATH', DEFAULT_DB_PATH)) as c:
    r = c.execute(\"SELECT value FROM app_settings WHERE key='paperless_api_token'\").fetchone()
    raise SystemExit(0 if r and r['value'] else 1)
" 2>/dev/null; then
    # Belt-and-suspenders: compose's `up -d` sometimes leaves a
    # dependent container in "Created" state when a healthcheck on
    # paperless-db / paperless-broker hasn't passed at the moment
    # paperless-web tries to start. Detect + start the laggard
    # explicitly so the wait below has something to wait for.
    PW_STATE=$(docker inspect -f '{{.State.Status}}' yorik-paperless-web 2>/dev/null || echo "missing")
    if [[ "$PW_STATE" == "created" || "$PW_STATE" == "exited" ]]; then
      warn "yorik-paperless-web is in '$PW_STATE' state — starting it manually"
      docker start yorik-paperless-web >/dev/null 2>&1 || true
    fi

    # First boot of paperless on a fresh DB applies ~50 Django
    # migrations + creates the search index + provisions the admin
    # user — easily 3–5 minutes on a slow machine. Wait up to 6 min,
    # with a progress nudge every 30s so the user doesn't think
    # we're stuck.
    say "PAPERLESS" "waiting for paperless to come up (up to 6 min on first boot)…"
    WAITED=0
    MAX_WAIT=360
    while (( WAITED < MAX_WAIT )); do
      if curl -fs http://localhost:8010/api/ >/dev/null 2>&1; then
        break
      fi
      sleep 5
      WAITED=$((WAITED + 5))
      if (( WAITED % 30 == 0 )); then
        # Show the user the most recent log line so they know it's
        # making progress (typically Django migration output).
        LAST=$(docker logs --tail 1 yorik-paperless-web 2>/dev/null | tr -d '\r')
        printf "  …still waiting (%ds) — last log: %s\n" "$WAITED" "${LAST:-(no output yet)}"
      fi
    done
    if curl -fs http://localhost:8010/api/ >/dev/null 2>&1; then
      ADMIN_USER=$(grep -E '^PAPERLESS_ADMIN_USER=' .env | cut -d= -f2-)
      TOKEN=$(docker exec yorik-paperless-web python manage.py shell -c "
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
u = get_user_model().objects.get(username='${ADMIN_USER}')
t, _ = Token.objects.get_or_create(user=u)
print(t.key)
" 2>/dev/null | tail -1 | tr -d '[:space:]')
      if [[ -n "$TOKEN" ]]; then
        python3 -c "
from backend.database import conn_ctx, DEFAULT_DB_PATH
from backend import credential_store
import os
# Mirror to app_settings (legacy lookup the iframe + reconciler use).
with conn_ctx(os.getenv('HOMEOS_DB_PATH', DEFAULT_DB_PATH)) as c:
    c.execute(\"\"\"INSERT INTO app_settings (key, value) VALUES ('paperless_api_token', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                   updated_at = datetime('now')\"\"\", ('$TOKEN',))
    c.execute(\"\"\"INSERT INTO app_settings (key, value) VALUES ('paperless_base_url', 'http://localhost:8010')
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                   updated_at = datetime('now')\"\"\")
# Mirror to credential_store — provision_paperless reads here first,
# and the startup auto-migration copies this row to Postgres when the
# operator flips backends. Without this, tenant signups skip
# Paperless with 'admin token not configured' on every multi-tenant
# install. Matches what the Immich bootstrap path already does.
credential_store.put('paperless', {
    'api_key':  '$TOKEN',
    'base_url': 'http://localhost:8010',
})
"
        ok "paperless API token stored — voice search ready"
      else
        warn "paperless token grab failed — set it manually in Settings → Connectors → Paperless"
      fi
    else
      warn "paperless not reachable after ${MAX_WAIT}s — check 'docker logs yorik-paperless-web'"
      warn "  (you can also run 'docker start yorik-paperless-web' + retry start.sh)"
    fi
  else
    skip "paperless API token already stored"
  fi

  # ────────────────────────── Immich admin bootstrap ──────────────────
  # Auto-create the Immich admin user via /api/auth/admin-sign-up and
  # generate an API key for Yorik. Stores the key in app_settings so
  # /api/auth/setup can provision per-Yorik-user Immich accounts.
  # Idempotent: if app_settings already has an immich_api_key, skip.
  HAVE_IMMICH_KEY=$(python3 -c "
try:
    from backend import credential_store
    creds = credential_store.get('immich') or {}
    print('yes' if creds.get('api_key') else 'no')
except Exception:
    print('no')
" 2>/dev/null)
  if [[ "$HAVE_IMMICH_KEY" == "no" ]] && [[ "${PROFILES[*]}" =~ bundled-immich ]]; then
    say "IMMICH" "bootstrapping admin + API key"
    IMMICH_WAITED=0
    while ! curl -fs --max-time 2 http://localhost:2283/api/server/ping >/dev/null 2>&1; do
      sleep 5
      IMMICH_WAITED=$((IMMICH_WAITED + 5))
      if (( IMMICH_WAITED >= 180 )); then
        warn "immich didn't come up within 180s — set admin key manually in Settings → Connectors → Immich"
        break
      fi
    done
    if curl -fs http://localhost:2283/api/server/ping >/dev/null 2>&1; then
      IMMICH_ADMIN_PW=$(openssl rand -hex 12)
      SIGNUP_RESP=$(curl -fs -X POST http://localhost:2283/api/auth/admin-sign-up \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"admin@yorik.local\",\"password\":\"${IMMICH_ADMIN_PW}\",\"name\":\"Yorik Admin\"}" 2>&1) || true
      if [[ -n "$SIGNUP_RESP" ]] && echo "$SIGNUP_RESP" | grep -q '"id"'; then
        # Log in to get a session token.
        LOGIN_RESP=$(curl -fs -X POST http://localhost:2283/api/auth/login \
          -H "Content-Type: application/json" \
          -d "{\"email\":\"admin@yorik.local\",\"password\":\"${IMMICH_ADMIN_PW}\"}")
        ACCESS_TOKEN=$(echo "$LOGIN_RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('accessToken', ''))" 2>/dev/null)
        if [[ -n "$ACCESS_TOKEN" ]]; then
          # Create an API key.
          KEY_RESP=$(curl -fs -X POST http://localhost:2283/api/api-keys \
            -H "Authorization: Bearer $ACCESS_TOKEN" \
            -H "Content-Type: application/json" \
            -d '{"name":"Yorik integration","permissions":["all"]}')
          IMMICH_KEY=$(echo "$KEY_RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('secret', ''))" 2>/dev/null)
          if [[ -n "$IMMICH_KEY" ]]; then
            # Store in credential_store (Fernet-encrypted) — that's where
            # backend/external_users.provision_immich reads from. Also
            # mirror into app_settings for the Settings UI to display.
            python3 -c "
from backend import credential_store
from backend.database import conn_ctx, DEFAULT_DB_PATH
import os
credential_store.put('immich', {
    'api_key':  '$IMMICH_KEY',
    'base_url': 'http://localhost:2283',
})
with conn_ctx(os.getenv('HOMEOS_DB_PATH', DEFAULT_DB_PATH)) as c:
    c.execute(\"\"\"INSERT INTO app_settings (key, value) VALUES ('immich_base_url', 'http://localhost:2283')
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                   updated_at = datetime('now')\"\"\")
"
            # Record the Immich admin password in .env so re-runs can recover.
            grep -qE "^IMMICH_ADMIN_PASSWORD=" .env || echo "IMMICH_ADMIN_PASSWORD=$IMMICH_ADMIN_PW" >> .env
            ok "immich admin + API key bootstrapped (admin@yorik.local)"
          else
            warn "immich api-key create failed — paste it manually in Settings → Connectors → Immich"
          fi
        else
          warn "immich admin login failed — paste API key manually in Settings → Connectors → Immich"
        fi
      else
        # admin-sign-up returns 400 if admin already exists; that's fine
        skip "immich admin user already exists or sign-up unavailable — paste API key manually if not configured"
      fi
    fi
  else
    [[ "$HAVE_IMMICH_KEY" == "yes" ]] && skip "immich API key already stored"
  fi
else
  warn "docker not found — skipping Immich + Paperless"
fi

# ─────────────────────────────────────────────────────────────────────
# Optional: offer to install a systemd service so Yorik auto-starts on
# boot and auto-restarts on crash. Skip silently when any precondition
# isn't met (non-interactive, non-systemd, already installed, etc.) —
# we don't want to nag returning users or break CI.

_should_offer_systemd() {
  [[ -t 0 ]] || return 1                                   # interactive only
  [[ -d /run/systemd/system ]] || return 1                 # systemd is PID 1
  command -v systemctl >/dev/null 2>&1 || return 1         # systemctl available
  ! systemctl list-unit-files 2>/dev/null | grep -q '^yorik.service' || return 1  # not already installed
  return 0
}

if _should_offer_systemd; then
  echo
  cat <<'EOPROMPT'
─────────────────────────────────────────────────────────
  Optional: install a systemd service?

  Without it, Yorik runs only while this shell stays open. If
  the box reboots, or Python crashes at 3am, you have to start
  it again by hand.

  With it (recommended for any always-on box):
    • auto-starts on boot
    • auto-restarts on crash (5s delay, 5-strike backoff)
    • logs to the journal (journalctl -u yorik -f)
    • sudo systemctl restart yorik  after `yorik upgrade`

  Sudo is needed once, to write /etc/systemd/system/yorik.service.
─────────────────────────────────────────────────────────
EOPROMPT
  read -r -p "Install yorik.service now? [y/N] " _yn
  if [[ "${_yn,,}" == "y" || "${_yn,,}" == "yes" ]]; then
    bash "$(dirname "$0")/scripts/install-systemd-service.sh" install || \
      warn "service install failed — retry later with: yorik service install"
  else
    echo "  → ok, skipping. Run later with: yorik service install"
  fi
fi

echo
cat <<EOF
─────────────────────────────────────────────────────────
Yorik / HomeOS running
  Dashboard  →  http://localhost:$PORT
  Immich     →  http://localhost:2283
  Paperless  →  http://localhost:8010
  LLM        →  $LLM_BASE_URL  (model: ${LLM_MODEL:-NOT SET — see Phase 2 warnings above})
  API log    →  $LOG
  API PID    →  $(cat /tmp/homeos-api.pid)

Stop the API:        kill \$(cat /tmp/homeos-api.pid)
Stop docker stack:   docker compose down  (Paperless + Immich + WhatsApp bridge)
─────────────────────────────────────────────────────────
EOF
