#!/bin/sh
# The all-in-one install's updater (the updater service in compose.yaml).
# Yorik's "Update now" writes /updater/request; this then
#
#   1. fetches the install files of the version .env asks for
#      (YORIK_VERSION: "stable" = the latest release, or a pinned
#      version) and puts them over the old ones — everything except .env
#      and backups/. Settings a new version adds to env.template are
#      appended to .env; new secrets get fresh random values.
#   2. pulls that version's images and recreates what changed.
#   3. recreates this updater last, from a helper container, in case the
#      new version changed it (a container can't recreate itself).
#
# "edge", "local" and other tags without a release skip step 1.
# Status for the app goes to /updater/status (one line, plain text).
#
# Paths: this container sees the install folder at /deploy, but the
# Docker engine needs the folder's real path on the computer for the
# bind mounts in compose.yaml; it's read from this container's own mount
# (on Windows that's Docker Desktop's /run/desktop/mnt/host/c/… form).
set -u
cd /deploy
RELEASES="${YORIK_RELEASES_URL:-https://github.com/winidi/yorik-ai/releases}"

status() { [ "${UPDATER_MODE:-}" = recreate ] || echo "$* $(date -Iseconds)" > /updater/status; }
envval() { grep -E "^$1=" /deploy/.env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r'; }
# This container's id: from the mounts Docker gives every container (works
# with any network mode), else the hostname.
SELF=$(grep -o '/containers/[0-9a-f]\{64\}/' /proc/self/mountinfo | head -1 | cut -d/ -f3)
me() { docker inspect "${SELF:-$(hostname)}" -f "$1"; }

HOST_DIR="$(me '{{range .Mounts}}{{if eq .Destination "/deploy"}}{{.Source}}{{end}}{{end}}')"
PROJECT="${UPDATER_PROJECT:-$(me '{{index .Config.Labels "com.docker.compose.project"}}')}"

# A Windows path from .env (C:\Users\…) as the engine in Docker Desktop's
# Linux VM sees it. Other paths pass through.
winpath() {
  case "$1" in
    [A-Za-z]:[\\/]*)
      d=$(printf %s "$1" | cut -c1 | tr 'A-Z' 'a-z')
      echo "/run/desktop/mnt/host/$d$(printf %s "$1" | cut -c3- | tr '\\' '/')" ;;
    *) echo "$1" ;;
  esac
}

dc() {
  files=""; sep=$(envval COMPOSE_PATH_SEPARATOR); cf=$(envval COMPOSE_FILE)
  old_ifs=$IFS; IFS=${sep:-:}
  for f in ${cf:-compose.yaml}; do files="$files -f /deploy/$f"; done
  IFS=$old_ifs
  bd=$(envval YORIK_BACKUP_DIR); [ -n "$bd" ] && export YORIK_BACKUP_DIR="$(winpath "$bd")"
  # shellcheck disable=SC2086
  docker compose -p "$PROJECT" --project-directory "$HOST_DIR" --env-file /deploy/.env $files "$@"
}

rand() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# Settings in the new env.template that .env doesn't have yet.
merge_env() {
  tail -c1 /deploy/.env | od -An -c | grep -q '\\n' || echo >> /deploy/.env
  added=""
  grep -E '^[A-Z][A-Z0-9_]*=' "$1" | while IFS= read -r line; do
    key=${line%%=*}; val=${line#*=}
    grep -qE "^$key=" /deploy/.env && continue
    case "$key" in *PASSWORD*|*SECRET*|*TOKEN*|*_KEY) [ -z "$val" ] && val=$(rand) ;; esac
    [ -z "$added" ] && { echo "# added by an update, $(date -I)" >> /deploy/.env; added=1; }
    echo "$key=$val" >> /deploy/.env
  done
}

# Step 1. Returns 0 when there's nothing to fetch for this version.
fetch_files() {
  v=$(envval YORIK_VERSION)
  case "${v:-stable}" in
    stable) url="$RELEASES/latest/download/yorik-deploy.zip" ;;
    v[0-9]*|[0-9]*) url="$RELEASES/download/v${v#v}/yorik-deploy.zip" ;;
    *) return 0 ;;
  esac
  rm -rf /tmp/new /tmp/new.zip; mkdir -p /tmp/new
  wget -q -O /tmp/new.zip "$url" || { FAIL="couldn't download $url"; return 1; }
  unzip -q /tmp/new.zip -d /tmp/new 2>/dev/null && [ -f /tmp/new/compose.yaml ] \
    || { FAIL="the download isn't a Yorik install bundle"; return 1; }
  owner=$(stat -c %u:%g /deploy/compose.yaml)
  # Stage every file next to its target first, then swap them all.
  list=$(cd /tmp/new && find . -type f ! -name .env ! -path './backups/*')
  old_ifs=$IFS; IFS='
'   # file names can have spaces ("Yorik Setup.command")
  for f in $list; do
    mkdir -p "/deploy/${f%/*}" && cp -p "/tmp/new/$f" "/deploy/$f.new" || { IFS=$old_ifs; FAIL="couldn't write $f"; return 1; }
  done
  for f in $list; do
    mv -f "/deploy/$f.new" "/deploy/$f"; chown "$owner" "/deploy/$f"
    d=${f%/*}; while [ "$d" != . ] && [ -n "$d" ]; do chown "$owner" "/deploy/$d"; d=${d%/*}; done
  done
  IFS=$old_ifs
  merge_env /tmp/new/env.template
  chown "$owner" /deploy/.env
  # Installs built from source before the first release switch to the
  # published images now that one exists.
  cf=$(envval COMPOSE_FILE)
  case "$cf" in *compose.build.yaml*)
    sep=$(envval COMPOSE_PATH_SEPARATOR); sep=${sep:-:}
    new=$(printf %s "$cf" | tr "$sep" '\n' | grep -vx compose.build.yaml | paste -sd "$sep")
    sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$new|" /deploy/.env ;;
  esac
  return 0
}

update() {
  FAIL=""
  # Without the real folder, compose would point every mount at /deploy.
  [ -n "$HOST_DIR" ] && [ -n "$PROJECT" ] || { status "failed couldn't find the install folder"; return; }
  status fetching
  fetch_files || { status "failed $FAIL"; return; }
  status pulling
  services=$(dc config --services | grep -vx updater | tr '\n' ' ')
  # shellcheck disable=SC2086
  if ! dc pull --quiet --ignore-buildable $services; then status "failed couldn't download the new images"; return; fi
  # shellcheck disable=SC2086
  if ! dc up -d --no-build --remove-orphans $services; then status "failed couldn't restart Yorik"; return; fi
  status done
  # Step 3, from a helper: recreate this container so the new updater.sh
  # runs (the running shell still has the old one in memory).
  docker run -d --rm -e UPDATER_MODE=recreate -e UPDATER_PROJECT="$PROJECT" \
    -v /var/run/docker.sock:/var/run/docker.sock -v "$HOST_DIR":/deploy -w /deploy \
    --entrypoint sh "$(me '{{.Config.Image}}')" /deploy/updater.sh >/dev/null
}

if [ "${UPDATER_MODE:-}" = recreate ]; then
  [ -n "$HOST_DIR" ] && [ -n "$PROJECT" ] || exit 1
  sleep 2
  dc up -d --no-build --force-recreate updater
  exit
fi

[ -f /updater/status ] || status idle
while true; do
  if [ -f /updater/request ]; then
    rm -f /updater/request
    update
  fi
  sleep 15
done
