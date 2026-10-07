#!/usr/bin/env bash
# Better "find by meaning" for the household search: Qwen3-Embedding in
# a llama.cpp container on the CPU. Downloads the model (2.5 GB) into
# models/embed/ and switches the search index to it in config.env.
# Then restart Yorik; the index rebuilds itself in the background.
#
#   bash scripts/install-search-embedder.sh            # 4B, the default
#   bash scripts/install-search-embedder.sh --off      # back to the bundled MiniLM
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG_FILE="config.env"
REPO="Qwen/Qwen3-Embedding-4B-GGUF"
FILE="Qwen3-Embedding-4B-Q4_K_M.gguf"
MODEL_TAG="qwen3-embedding-4b"
PORT="${YORIK_SEARCH_EMBED_PORT:-8093}"

set_env() {
  local key="$1" value="$2"
  if grep -qE "^#? ?${key}=" "$CONFIG_FILE"; then
    sed -i.bak -E "0,/^#? ?${key}=.*/s||${key}=${value}|" "$CONFIG_FILE" && rm -f "$CONFIG_FILE.bak"
  else
    echo "${key}=${value}" >> "$CONFIG_FILE"
  fi
}

[[ -f "$CONFIG_FILE" ]] || { echo "config.env not found — run install.sh / start.sh once first" >&2; exit 1; }

if [[ "${1:-}" == "--off" ]]; then
  set_env YORIK_SEARCH_EMBED 0
  sed -i.bak -E "s|^(YORIK_SEARCH_EMBED_URL=.*)|# \1|" "$CONFIG_FILE" && rm -f "$CONFIG_FILE.bak"
  docker rm -f yorik-search-embed >/dev/null 2>&1 || true
  echo "Search is back on the bundled embedder. Restart Yorik; the index rebuilds itself."
  exit 0
fi

mkdir -p models/embed
if [[ ! -f "models/embed/$FILE" ]]; then
  echo "Downloading $FILE (2.5 GB) …"
  curl -fL --retry 3 -C - -o "models/embed/$FILE.part" "https://huggingface.co/$REPO/resolve/main/$FILE"
  mv "models/embed/$FILE.part" "models/embed/$FILE"
fi

set_env YORIK_SEARCH_EMBED 1
set_env YORIK_SEARCH_EMBED_URL "http://127.0.0.1:${PORT}/v1"
set_env YORIK_SEARCH_EMBED_MODEL "$MODEL_TAG"
set_env YORIK_SEARCH_EMBED_FILE "$FILE"
# An NVIDIA card with room (4 GB free) takes the model off the RAM: the
# CUDA image and all layers on the GPU. Needs nvidia-container-toolkit
# and YORIK_IMMICH_GPU=nvidia (start.sh layers docker-compose.nvidia.yml).
if command -v nvidia-smi >/dev/null 2>&1; then
  free_mib="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
  if [[ "${free_mib:-0}" -ge 4000 ]]; then
    set_env YORIK_SEARCH_EMBED_IMAGE "ghcr.io/ggml-org/llama.cpp:server-cuda"
    set_env YORIK_SEARCH_EMBED_NGL 99
    echo "NVIDIA GPU with ${free_mib} MiB free: the embedder will run on it (set YORIK_IMMICH_GPU=nvidia in config.env if it is not)."
  else
    echo "NVIDIA GPU found but only ${free_mib:-0} MiB free: the embedder stays on the CPU."
  fi
fi
echo "The service sleeps between index runs (Settings › Search by meaning: at night, every hour, or continuously)."

echo "Done. Restart Yorik (sudo systemctl restart yorik, or bash start.sh)."
echo "The worker \"search-index\" on the home screen shows the rebuild."
