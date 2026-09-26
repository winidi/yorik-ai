#!/bin/sh
# The all-in-one install's updater (see the updater service in
# compose.yaml). Yorik's "Update now" writes /updater/request; this
# pulls the images of this compose project and recreates what changed.
# Status for the app goes to /updater/status (one line, plain text).
set -u
cd /deploy
echo "idle $(date -Iseconds)" > /updater/status
while true; do
  if [ -f /updater/request ]; then
    rm -f /updater/request
    echo "pulling $(date -Iseconds)" > /updater/status
    if docker compose pull --quiet --ignore-buildable && docker compose up -d --remove-orphans; then
      echo "done $(date -Iseconds)" > /updater/status
    else
      echo "failed $(date -Iseconds)" > /updater/status
    fi
  fi
  sleep 15
done
