#!/usr/bin/env bash
# A second look at the search hits: bge-reranker-v2-m3 in a llama.cpp
# container on the CPU (backend/search_rerank.py). Downloads the model
# (0.6 GB) into models/rerank/ and switches it on in config.env. Then
# restart Yorik.
#
#   bash scripts/install-search-reranker.sh            # install and switch on
#   bash scripts/install-search-reranker.sh --off      # hits keep the search's order
set -euo pipefail
cd "$(dirname "$0")/.."
CONFIG_FILE="config.env"
REPO="gpustack/bge-reranker-v2-m3-GGUF"
FILE="bge-reranker-v2-m3-Q8_0.gguf"
PORT="${YORIK_SEARCH_RERANK_PORT:-8094}"

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
  set_env YORIK_SEARCH_RERANK 0
  sed -i.bak -E "s|^(YORIK_SEARCH_RERANK_URL=.*)|# \1|" "$CONFIG_FILE" && rm -f "$CONFIG_FILE.bak"
  docker rm -f yorik-search-rerank >/dev/null 2>&1 || true
  echo "The reranker is off. Restart Yorik; hits keep the search's order."
  exit 0
fi

mkdir -p models/rerank
if [[ ! -f "models/rerank/$FILE" ]]; then
  echo "Downloading $FILE (0.6 GB) …"
  curl -fL --retry 3 -C - -o "models/rerank/$FILE.part" "https://huggingface.co/$REPO/resolve/main/$FILE"
  mv "models/rerank/$FILE.part" "models/rerank/$FILE"
fi
set_env YORIK_SEARCH_RERANK 1
set_env YORIK_SEARCH_RERANK_URL "http://127.0.0.1:${PORT}/v1"
set_env YORIK_SEARCH_RERANK_FILE "$FILE"
echo "Done. Restart Yorik (sudo systemctl restart yorik, or bash start.sh)."
