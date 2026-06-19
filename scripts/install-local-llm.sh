#!/usr/bin/env bash
# Optional: install a bundled local LLM (llama.cpp + Qwen3.5 GGUFs).
#
# Yorik's default flow is bring-your-own-LLM: configure any OpenAI-
# compatible endpoint (Ollama, LM Studio, llama.cpp, vLLM) in
# Settings → LLM. This script is for users who want Yorik to download
# and run the LLM itself, end-to-end, with no other dependencies.
#
# What it does:
#   • picks a Qwen3.5 size by RAM (0.8B → 27B) — override via HOMEOS_CHAT_SIZE
#   • downloads llama.cpp release binaries from GitHub
#   • downloads the chat + embedding GGUFs from HuggingFace
#   • starts two llama-server processes (chat :8082, embed :8083)
#   • patches config.env so Yorik points at them
#
# Idempotent: re-run to upgrade llama.cpp or swap model sizes.

set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG_FILE="config.env"
[[ -f "$CONFIG_FILE" ]] || cp config.env.example "$CONFIG_FILE"
# shellcheck disable=SC1090
set -a; source "$CONFIG_FILE"; set +a

say()  { printf "\033[36m[%s]\033[0m %s\n" "$1" "$2"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
skip() { printf "  \033[33m·\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m![\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; exit 1; }

say "LOCAL LLM" "bundled llama.cpp + Qwen3.5"

LLM_CHAT_PORT="${YORIK_CHAT_PORT:-8082}"
LLM_EMBED_PORT="${YORIK_EMBED_PORT:-8083}"
LLM_CHAT_URL="http://127.0.0.1:${LLM_CHAT_PORT}/v1"
LLM_EMBED_URL="http://127.0.0.1:${LLM_EMBED_PORT}/v1"

CHAT_QUANT="${HOMEOS_CHAT_QUANT:-Q4_K_M}"

if [[ -z "${YORIK_CHAT_MODEL_REPO:-}" ]]; then
  CHAT_SIZE_HINT="${HOMEOS_CHAT_SIZE:-auto}"
  if [[ "$CHAT_SIZE_HINT" == "auto" ]]; then
    RAM_GB=$(awk '/^MemTotal:/ {printf "%d", $2 / 1024 / 1024}' /proc/meminfo 2>/dev/null || echo 8)
    if   (( RAM_GB <  8 )); then CHAT_SIZE_HINT="4b"
    elif (( RAM_GB < 24 )); then CHAT_SIZE_HINT="9b"
    else                         CHAT_SIZE_HINT="9b"  # 27B is opt-in
    fi
    say "LOCAL LLM" "auto-picked Qwen3.5-${CHAT_SIZE_HINT^^} for ${RAM_GB} GB RAM (override: HOMEOS_CHAT_SIZE=0.8b|2b|4b|9b|27b)"
  fi
  case "$CHAT_SIZE_HINT" in
    0.8b|0.8B) YORIK_CHAT_MODEL_REPO="unsloth/Qwen3.5-0.8B-GGUF"
               YORIK_CHAT_MODEL_FILE="Qwen3.5-0.8B-${CHAT_QUANT}.gguf"
               _DEFAULT_CTX=8192 ;;
    2b|2B)     YORIK_CHAT_MODEL_REPO="unsloth/Qwen3.5-2B-MTP-GGUF"
               YORIK_CHAT_MODEL_FILE="Qwen3.5-2B-${CHAT_QUANT}.gguf"
               _DEFAULT_CTX=8192 ;;
    4b|4B)     YORIK_CHAT_MODEL_REPO="unsloth/Qwen3.5-4B-MTP-GGUF"
               YORIK_CHAT_MODEL_FILE="Qwen3.5-4B-${CHAT_QUANT}.gguf"
               _DEFAULT_CTX=16384 ;;
    9b|9B)     YORIK_CHAT_MODEL_REPO="unsloth/Qwen3.5-9B-MTP-GGUF"
               YORIK_CHAT_MODEL_FILE="Qwen3.5-9B-${CHAT_QUANT}.gguf"
               _DEFAULT_CTX=32768 ;;
    27b|27B)   YORIK_CHAT_MODEL_REPO="unsloth/Qwen3.5-27B-MTP-GGUF"
               YORIK_CHAT_MODEL_FILE="Qwen3.5-27B-${CHAT_QUANT}.gguf"
               _DEFAULT_CTX=32768 ;;
    *) fail "unknown HOMEOS_CHAT_SIZE=$CHAT_SIZE_HINT (use 0.8b, 2b, 4b, 9b, or 27b)" ;;
  esac
fi

case "${YORIK_CHAT_MODEL_REPO}" in
  *MTP-GGUF) CHAT_MTP_FLAGS=(--spec-draft-n-max 2) ;;
  *)         CHAT_MTP_FLAGS=() ;;
esac

NUM_CTX="${HOMEOS_LLM_NUM_CTX:-${_DEFAULT_CTX:-32768}}"
CHAT_REPO="${YORIK_CHAT_MODEL_REPO}"
CHAT_FILE="${YORIK_CHAT_MODEL_FILE}"
EMBED_REPO="${YORIK_EMBED_MODEL_REPO:-nomic-ai/nomic-embed-text-v1.5-GGUF}"
EMBED_FILE="${YORIK_EMBED_MODEL_FILE:-nomic-embed-text-v1.5.Q4_K_M.gguf}"

MODELS_DIR="$(pwd)/data/models"
CHAT_GGUF="$MODELS_DIR/$(basename "$CHAT_REPO")/${CHAT_FILE}"
EMBED_GGUF="$MODELS_DIR/$(basename "$EMBED_REPO")/${EMBED_FILE}"
mkdir -p "$MODELS_DIR"

# ── llama.cpp binary ─────────────────────────────────────────────────
_have_llama_server() { command -v llama-server >/dev/null 2>&1; }

if _have_llama_server; then
  skip "llama-server already on PATH: $(command -v llama-server)"
else
  say "LOCAL LLM" "installing llama.cpp"
  SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    command -v brew >/dev/null 2>&1 || fail "brew not found — install from https://brew.sh"
    brew install llama.cpp
  else
    if $SUDO apt-get install -y -qq llama.cpp 2>/dev/null && _have_llama_server; then
      ok "llama.cpp installed via apt"
    else
      case "$(uname -m)" in
        x86_64|amd64)  DEFAULT_ARCH="x64"   ;;
        aarch64|arm64) DEFAULT_ARCH="arm64" ;;
        *) fail "unsupported CPU arch '$(uname -m)'" ;;
      esac
      LLAMA_ARCH="${YORIK_LLAMA_ARCH:-$DEFAULT_ARCH}"
      DL_DIR="$(pwd)/data/llama.cpp"
      mkdir -p "$DL_DIR"
      rm -f "$DL_DIR/llama.tar.gz"
      touch "$DL_DIR/.write-probe" 2>/dev/null || fail "can't write to $DL_DIR (sudo chown -R $USER $DL_DIR)"
      rm -f "$DL_DIR/.write-probe"
      FREE_MB=$(df -Pm "$DL_DIR" | awk 'NR==2 {print $4}')
      [[ "${FREE_MB:-0}" -lt 300 ]] && fail "only ${FREE_MB}MB free — need ~300MB"
      LATEST=$(curl -fsSL https://api.github.com/repos/ggml-org/llama.cpp/releases/latest \
                 | grep -m1 '"tag_name"' | cut -d'"' -f4)
      [[ -z "$LATEST" ]] && fail "couldn't fetch llama.cpp tag (rate-limited?). retry or set YORIK_LLAMA_TAG=bXXXX"
      LLAMA_TAG="${YORIK_LLAMA_TAG:-$LATEST}"
      ASSET="llama-${LLAMA_TAG}-bin-ubuntu-${LLAMA_ARCH}.tar.gz"
      URL="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_TAG}/${ASSET}"
      say "LOCAL LLM" "downloading $ASSET (~150 MB)"
      curl -fL --retry 3 --connect-timeout 10 -o "$DL_DIR/llama.tar.gz" "$URL" \
        || fail "download failed — try: wget $URL -O $DL_DIR/llama.tar.gz"
      tar -xzf "$DL_DIR/llama.tar.gz" -C "$DL_DIR"
      LCPP_BIN=$(find "$DL_DIR" -name 'llama-server' -type f -executable 2>/dev/null | head -1)
      [[ -x "$LCPP_BIN" ]] || fail "extracted archive but llama-server not found"
      LCPP_LIB_DIR=$(dirname "$LCPP_BIN")
      $SUDO tee /usr/local/bin/llama-server > /dev/null <<WRAPPER
#!/usr/bin/env bash
LD_LIBRARY_PATH="$LCPP_LIB_DIR:\${LD_LIBRARY_PATH:-}" exec "$LCPP_BIN" "\$@"
WRAPPER
      $SUDO chmod +x /usr/local/bin/llama-server
      ok "llama.cpp $LATEST installed (wrapper at /usr/local/bin/llama-server)"
    fi
  fi
  _have_llama_server || fail "llama-server still not on PATH"
fi

# ── GGUFs from HuggingFace ───────────────────────────────────────────
if [[ -f "$CHAT_GGUF" ]] && [[ -f "$EMBED_GGUF" ]]; then
  skip "GGUFs already present at $MODELS_DIR"
else
  command -v huggingface-cli >/dev/null 2>&1 || {
    say "LOCAL LLM" "installing huggingface_hub CLI"
    python3 -m pip install --quiet --user 'huggingface_hub[cli]>=0.26' \
      || fail "huggingface_hub install failed — pip install huggingface_hub manually"
    export PATH="$HOME/.local/bin:$PATH"
  }
  if [[ ! -f "$CHAT_GGUF" ]]; then
    say "LOCAL LLM" "downloading chat GGUF $CHAT_REPO ($CHAT_FILE)"
    huggingface-cli download "$CHAT_REPO" "$CHAT_FILE" \
      --local-dir "$MODELS_DIR/$(basename "$CHAT_REPO")" \
      || fail "chat GGUF download failed"
  fi
  if [[ ! -f "$EMBED_GGUF" ]]; then
    say "LOCAL LLM" "downloading embedding GGUF $EMBED_REPO ($EMBED_FILE)"
    huggingface-cli download "$EMBED_REPO" "$EMBED_FILE" \
      --local-dir "$MODELS_DIR/$(basename "$EMBED_REPO")" \
      || fail "embedding GGUF download failed"
  fi
fi

# ── Start llama-server processes (chat + embed) ──────────────────────
_start_llama() {
  local label="$1" pidfile="$2" logfile="$3" port="$4" gguf="$5"; shift 5
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null \
       && curl -fs --max-time 1 "http://127.0.0.1:${port}/v1/models" >/dev/null; then
    skip "${label} already running on :${port}"
    return 0
  fi
  command -v fuser >/dev/null 2>&1 && fuser -k "${port}/tcp" 2>/dev/null || true
  say "LOCAL LLM" "starting ${label} on :${port}"
  nohup llama-server "$@" --model "$gguf" --host 127.0.0.1 --port "$port" \
    >"$logfile" 2>&1 &
  echo $! > "$pidfile"
  for _ in $(seq 1 20); do
    if curl -fs --max-time 1 "http://127.0.0.1:${port}/v1/models" >/dev/null; then
      ok "${label} ready on :${port} (pid $(cat "$pidfile"), log: ${logfile})"
      return 0
    fi
    sleep 1
  done
  warn "${label} took >20s — check ${logfile}"
}

_start_llama "chat (Qwen3.5)" /tmp/yorik-chat.pid /tmp/yorik-chat.log \
  "$LLM_CHAT_PORT" "$CHAT_GGUF" -c "$NUM_CTX" --jinja "${CHAT_MTP_FLAGS[@]}"
_start_llama "embed (nomic)" /tmp/yorik-embed.pid /tmp/yorik-embed.log \
  "$LLM_EMBED_PORT" "$EMBED_GGUF" --embeddings

# ── Patch config.env to point at the bundled servers ─────────────────
_set_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$CONFIG_FILE"; then
    sed -i.bak -E "s|^${key}=.*|${key}=${value}|" "$CONFIG_FILE"
  else
    echo "${key}=${value}" >> "$CONFIG_FILE"
  fi
}
_set_env HOMEOS_LLM_BASE_URL    "$LLM_CHAT_URL"
_set_env HOMEOS_MODEL           "$CHAT_FILE"
_set_env HOMEOS_EMBED_BASE_URL  "$LLM_EMBED_URL"
_set_env HOMEOS_EMBED_MODEL     "$EMBED_FILE"
rm -f "$CONFIG_FILE.bak"
ok "config.env → CHAT=$LLM_CHAT_URL, EMBED=$LLM_EMBED_URL"

echo
echo "Bundled local LLM ready. Restart Yorik (or just reload the page) — "
echo "Settings → LLM will show the new endpoint as reachable."
