#!/usr/bin/env bash
# Validate an e-invoice PDF with Mustang, the reference validator of the
# ZUGFeRD project: PDF/A-3 (veraPDF) and the full EN 16931 rule set.
# Yorik's own check (backend/writing/einvoice.py) covers the schema and
# the sums; this is the second opinion before trusting a change.
#
#   scripts/validate_einvoice.sh invoice.pdf
#
# Needs Java and the Mustang CLI jar (github.com/ZUGFeRD/mustangproject,
# "Mustang-CLI-x.y.z.jar"); point YORIK_MUSTANG_JAR at it.
set -euo pipefail
pdf="${1:?usage: validate_einvoice.sh <invoice.pdf>}"
jar="${YORIK_MUSTANG_JAR:?set YORIK_MUSTANG_JAR to the Mustang-CLI jar}"
out="$(mktemp)"; trap 'rm -f "$out"' EXIT
java -Xmx1G -jar "$jar" --no-notices --action validate --source "$pdf" >"$out" 2>/dev/null || true
if grep -q '<summary status="invalid"/>' "$out"; then
  echo "INVALID"
  { grep -o '\[BR[^]]*\][^<]*' "$out" | sort -u | head -20; } || true
  { grep -o 'clause=[0-9.]*, testNumber=[0-9]*\], status=failed, message=[^,]*' "$out" | sort | uniq -c | head -20; } || true
  exit 1
fi
grep -q '<summary status="valid"/>' "$out" && echo "VALID (PDF/A-3 and EN 16931 rules)"
