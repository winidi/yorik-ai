#!/usr/bin/env bash
# Pre-build Tailwind CSS for Yorik. Same pattern as scripts/build-tiptap-vendor.sh:
# the maintainer runs this once when adding/removing utility classes; end users
# never run it. Output lands in frontend/vendor/tailwind.css and is committed.
#
# Tailwind v4's CLI scans the source files declared in tailwind-entry.css's
# @source directive and only emits the utility classes that are actually
# referenced — usually ~40-100 KB once gzipped.
#
# Requires: node + npx (maintainer's box only).

set -euo pipefail
cd "$(dirname "$0")/.."

OUT=frontend/vendor/tailwind.css
mkdir -p frontend/vendor node_modules_tailwind_build
cd node_modules_tailwind_build

if [[ ! -f package.json ]]; then
  cat > package.json <<'EOF'
{
  "name": "yorik-tailwind-build",
  "private": true,
  "version": "0.0.1",
  "dependencies": {
    "tailwindcss": "^4.1.0",
    "@tailwindcss/cli": "^4.1.0"
  }
}
EOF
fi

npm install --silent --no-audit --no-fund

# Copy entry CSS in so the v4 CLI resolves tailwindcss from our local
# node_modules. Source paths in @source remain relative to repo root via
# the "../" prefix.
cp ../scripts/tailwind-entry.css ./entry.css

./node_modules/.bin/tailwindcss \
  -i ./entry.css \
  -o "../$OUT" \
  --minify

cd ..
SIZE=$(du -h "$OUT" | cut -f1)
echo "✓ $OUT ($SIZE)"
