# Minimal Node.js image for the Baileys WhatsApp bridge.
# Session state lives in /data which is mounted as a volume by compose so
# we don't re-pair via QR on every restart.

FROM node:20-alpine

# Baileys pulls some deps from git (and a couple have native bindings),
# so the build needs git + a small C/C++ toolchain. Install in a single
# layer; nothing pinned because alpine's package set is curated.
RUN apk add --no-cache git python3 make g++ \
 && ln -sf python3 /usr/bin/python

WORKDIR /app

# Install deps first for better layer caching.
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

# Then the server itself.
COPY server.js ./

# Bridge listens on 3001 internally; the compose file maps it.
EXPOSE 3001

# Persistent auth state ends up here.
VOLUME ["/data"]

CMD ["node", "server.js"]
