# Yorik as a container: the app (FastAPI + the React bundle) with every
# Python dependency baked in, so the host needs Docker and nothing else
# of Yorik's own. The database, photos, documents and WhatsApp keep
# running in their own containers exactly as in the classic install.
#
#   bash install.sh --container        builds this image and runs it
#   docker compose -f docker-compose.app.yml up -d --build yorik
#
# Runs with host networking (docker-compose.app.yml) so it reaches the
# database on :5435, the AI model on :8080/:11434 and the other services
# on localhost exactly like the classic install, and `tailscale serve`
# keeps pointing at localhost:8000. data/ and config.env stay on the host.

FROM node:22-alpine AS web
# bash + coreutils for the build's fingerprint step (scripts/fingerprint.sh)
RUN apk add --no-cache bash coreutils findutils
WORKDIR /build
COPY frontend-react/package.json frontend-react/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend-react/ ./
RUN npm run build

FROM python:3.12-slim
# ffmpeg: audio decoding for speech-to-text; libgomp1: onnxruntime;
# tini: a proper PID 1; git: the in-app version check reads the image's
# version file instead, but some deps install from VCS.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgomp1 curl ca-certificates tini git build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip wheel \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/backend/requirements.txt \
    && apt-get purge -y build-essential && apt-get autoremove -y
COPY . /app/
COPY --from=web /build/dist /app/frontend-react/dist/
ENV PATH="/opt/venv/bin:${PATH}" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    YORIK_RUNTIME=container
VOLUME ["/app/data"]
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
