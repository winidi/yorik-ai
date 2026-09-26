#!/usr/bin/env bash
# Container start: fetch the speech/voice/search models once into the
# data volume (same steps as start.sh PHASE 4), bring the schema up to
# date, then run Yorik. The database stack itself is started on the host
# by start.sh (scripts/bootstrap-supabase.sh) before this container.
set -euo pipefail
cd /app
mkdir -p data "${HOMEOS_VOICES_DIR:-data/voices}"
export HF_HUB_DISABLE_PROGRESS_BARS=1

fetch() {  # label python-check python-download
  if python -c "import sys; $2" 2>/dev/null; then echo "[models] $1 present"
  else echo "[models] downloading $1 (one-time)"; python -c "$3" || echo "[models] $1 failed — Yorik starts anyway"; fi
}
fetch "speech-to-text" "from backend import stt_parakeet as p; sys.exit(0 if p.installed() else 1)" \
                      "from backend import stt_parakeet as p; p.download(p.current_variant())"
fetch "voice"          "import glob,os; sys.exit(0 if glob.glob(os.path.join(os.getenv('HOMEOS_VOICES_DIR','data/voices'),'supertonic-3','**','*.onnx'), recursive=True) else 1)" \
                      "from backend.tts import warm_up; warm_up()"
fetch "speaker id"     "from backend import voice_id as v; sys.exit(0 if v.installed() else 1)" \
                      "from backend import voice_id as v; v.download()"
fetch "search"         "from backend.embedders import local as e; sys.exit(0 if e.installed() else 1)" \
                      "from backend.embedders import local as e; e.download()"
python -c "from backend import voice_acks; voice_acks.warmup()" >/dev/null 2>&1 || true

# Wait for the database (host :5435) and bring the schema up to date.
for i in $(seq 1 60); do
  python -m backend.database >/dev/null 2>&1 && { echo "[db] schema current"; break; }
  [[ $i == 60 ]] && { echo "[db] database not reachable on :5435 — is the Supabase stack up?"; exit 1; }
  echo "[db] waiting for the database…"; sleep 3
done

# All-in-one install: connect photos, documents and the AI model once
# they're up (backend/docker_bootstrap.py; idempotent, in the background).
if [[ "${YORIK_RUNTIME:-}" == "docker" ]]; then
  python -m backend.docker_bootstrap &
fi

exec uvicorn backend.main:app --host "${YORIK_BIND:-0.0.0.0}" --port "${HOMEOS_PORT:-8000}" \
     --proxy-headers --forwarded-allow-ips 127.0.0.1
