#!/usr/bin/env bash
# Pre-bundle TipTap + the extensions Yorik's Compose app uses into a single
# IIFE that exposes a `window.Tiptap` namespace. End users NEVER run this —
# the resulting frontend/vendor/tiptap.bundle.js is committed to the repo.
# Re-run only when bumping TipTap versions or adding/removing extensions
# in scripts/tiptap-entry.js.
#
# Requires: node + npx (only on the maintainer's box).
# Produces: frontend/vendor/tiptap.bundle.js (~150-250KB minified).

set -euo pipefail
cd "$(dirname "$0")/.."

OUT=frontend/vendor/tiptap.bundle.js
mkdir -p frontend/vendor

# Install just for the bundle (kept out of the repo via .gitignore).
mkdir -p node_modules_tiptap_build
cd node_modules_tiptap_build
if [[ ! -f package.json ]]; then
  cat > package.json <<'EOF'
{
  "name": "yorik-tiptap-build",
  "private": true,
  "version": "0.0.1",
  "dependencies": {
    "@tiptap/core": "^2.10.0",
    "@tiptap/starter-kit": "^2.10.0",
    "@tiptap/extension-underline": "^2.10.0",
    "@tiptap/extension-link": "^2.10.0",
    "@tiptap/extension-placeholder": "^2.10.0",
    "@tiptap/extension-mention": "^2.10.0",
    "@tiptap/extension-table": "^2.10.0",
    "@tiptap/extension-table-row": "^2.10.0",
    "@tiptap/extension-table-header": "^2.10.0",
    "@tiptap/extension-table-cell": "^2.10.0",
    "@tiptap/pm": "^2.10.0",
    "esbuild": "^0.24.0"
  }
}
EOF
fi

npm install --silent --no-audit --no-fund

# Copy the entry file in so esbuild resolves node_modules correctly.
cp ../scripts/tiptap-entry.js ./entry.js
./node_modules/.bin/esbuild \
  --bundle --minify --format=iife --global-name=Tiptap \
  --target=es2020 \
  --outfile="../$OUT" \
  entry.js

cd ..
SIZE=$(du -h "$OUT" | cut -f1)
echo "✓ $OUT ($SIZE)"
