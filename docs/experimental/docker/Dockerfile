# Yorik backend image — runs the FastAPI app + serves the React bundle.
#
# Two stages:
#   1. node-build — builds the React frontend (vite + tsc) so we don't
#      need Node in the runtime image.
#   2. runtime — Python 3.12-slim + system deps + venv with pinned
#      requirements + the built bundle.
#
# Container layout:
#   /app/backend                  Python source
#   /app/frontend-react/dist      React bundle (served by StaticFiles)
#   /app/data                     VOLUME — bind-mount the user's data dir
#                                 here. Lives on the host so DB + photos
#                                 survive container rebuilds.
#   /app/config.env               Bound in from the host so the user
#                                 keeps their tokens out of the image.
#
# llama-swap is NOT bundled. The host (or another container the user
# manages) runs it; set HOMEOS_LLM_BASE_URL in config.env to point at
# the right endpoint. From inside the Yorik container, the host's
# 127.0.0.1:8080 is reachable via `host.docker.internal:8080` when the
# compose file sets `extra_hosts`.

# ── Stage 1: build the React bundle ────────────────────────────────────
FROM node:22-alpine AS node-build

WORKDIR /build

# Cache the npm install layer by copying lockfile first.
COPY frontend-react/package.json frontend-react/package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

# Now copy the rest of the React source and build. The resulting
# dist/ is what the runtime image will serve.
COPY frontend-react/ ./
RUN npm run build


# ── Stage 2: runtime ───────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# System deps:
#   ffmpeg          → whisper STT decodes any audio container
#   curl            → healthchecks + diagnostics
#   ca-certificates → SSL to satellite services (Paperless, etc.)
#   tini            → PID 1 reaping (uvicorn's child workers leave zombies)
#   git             → some Python deps build from VCS during install
#   build-essential → sqlite-vec + a few wheels still build native
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates tini git build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps in a venv inside the image. Cache the pip layer
# via BuildKit so a requirements.txt edit doesn't redownload torch etc.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip wheel \
    && /opt/venv/bin/pip install -r /app/backend/requirements.txt

# Backend source + migrations + scripts.
COPY backend/      /app/backend/
COPY migrations/   /app/migrations/
COPY scripts/      /app/scripts/

# React bundle from stage 1.
COPY --from=node-build /build/dist /app/frontend-react/dist/

# /app/data is a VOLUME — the user binds either a local dir or an
# external SSD. The storage.py migration helper deals with what
# lives inside.
VOLUME ["/app/data"]
EXPOSE 8000

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOMEOS_PORT=8000

# Use tini as PID 1 so uvicorn's worker subprocess + the websocket
# subscribers exit cleanly on docker stop. Without it you get zombies
# and a 10s SIGKILL delay on restart.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
