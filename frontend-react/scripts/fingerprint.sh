#!/usr/bin/env bash
# Print a sha256 fingerprint of every file under frontend-react/ that
# influences the Vite build output. start.sh recomputes this at boot and
# compares against frontend-react/dist/.src-fingerprint (written here
# by the postbuild script in package.json) — mismatch means dist/ is
# stale and the maintainer forgot `npm run build`.
#
# Why fingerprint over mtime: `git clone` resets every file's mtime to
# checkout-time, so mtime checks false-positive on fresh installs and
# false-negative across pulls. Content hashing catches both cases.
#
# Excluded: dist/ (the artifact we're checking), node_modules/ (huge,
# platform-specific, doesn't affect the build deterministically), the
# .tsbuildinfo TypeScript incremental cache (regenerated every build),
# and dotfiles (.git, .DS_Store).
set -euo pipefail
cd "$(dirname "$0")/.."

find . -type f \
    -not -path './dist/*' \
    -not -path './node_modules/*' \
    -not -path './.git/*' \
    -not -name '*.tsbuildinfo' \
    -not -name '.*' \
    -print0 \
  | sort -z \
  | xargs -0 sha256sum \
  | sha256sum \
  | awk '{print $1}'
