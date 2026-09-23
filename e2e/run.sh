#!/usr/bin/env bash
# One command for the whole check: build the test household, crawl every
# page as parent, second parent and child on desktop and phone, throw the
# household away again. The report is e2e/report/SUMMARY.md.
#
#   bash e2e/run.sh              # everything (about 10 minutes)
#   bash e2e/run.sh clara phone  # one person, one device
#   KEEP=1 bash e2e/run.sh       # leave the household running afterwards
#                                # (http://127.0.0.1:8177, password testhaus-2026)
set -uo pipefail
cd "$(dirname "$0")/.."

[[ -d e2e/node_modules ]] || (cd e2e && npm install --no-audit --no-fund >/dev/null && npx playwright install chromium >/dev/null)

venv/bin/python e2e/household.py up || exit 2
(cd e2e && node crawl.mjs "$@")
status=$?
[[ "${KEEP:-0}" == "1" ]] || venv/bin/python e2e/household.py down

echo
echo "Report: e2e/report/SUMMARY.md (screenshots in e2e/report/shots/)"
[[ -s e2e/.run/blocked.log ]] && echo "The test server tried to reach (and was refused): $(awk '{print $3}' e2e/.run/blocked.log | sort -u | tr '\n' ' ')"
exit $status
