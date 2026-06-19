#!/usr/bin/env bash
# Paperless POST_CONSUME_SCRIPT — runs inside the paperless-web container
# every time a document finishes processing. Env vars Paperless provides:
#   DOCUMENT_ID — the new doc's primary key
#   DOCUMENT_FILE_NAME — original filename
# Env vars we inject via docker-compose:
#   YORIK_INGEST_URL — http://host.docker.internal:8000/api/paperless/ingest
#   YORIK_PAPERLESS_TOKEN — shared secret so Yorik can trust this is from Paperless
#
# Yorik then fetches the OCR'd text + metadata via Paperless's REST API,
# chunks it, embeds it locally, and stores vectors in sqlite-vec.
# Failures here are best-effort: if Yorik is down, the doc is still in
# Paperless; reindex-all can backfill later.

set -e
curl -s -X POST \
    --max-time 5 \
    -H "X-Paperless-Token: ${YORIK_PAPERLESS_TOKEN}" \
    "${YORIK_INGEST_URL}/${DOCUMENT_ID}" \
    >/dev/null 2>&1 || true
