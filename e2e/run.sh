#!/usr/bin/env bash
# The whole check in one command: build the test household, crawl every
# page, run the journeys (server and on screen), throw the household away.
# One page to read afterwards: e2e/report/README.md. Each run is also kept
# under e2e/history/<date-time>/.
#
#   bash e2e/run.sh                # everything (about 15 minutes)
#   bash e2e/run.sh clara phone    # crawler only, one person, one device
#   KEEP=1 bash e2e/run.sh         # leave the household running afterwards
#                                  # (http://127.0.0.1:8177, password testhaus-2026)
#   FAKE_LLM=1 bash e2e/run.sh     # never use the real model
#
# The real model is used when it answers on 127.0.0.1:8080 (read-only
# inference; what the chat creates lands in the throwaway database).
# With a token in ~/.config/yorik-e2e/token the verdict also lands in
# that person's Yorik bell (see e2e/README.md).
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

[[ -d e2e/node_modules ]] || (cd e2e && npm install --no-audit --no-fund >/dev/null && npx playwright install chromium >/dev/null)

if [[ "${FAKE_LLM:-0}" != "1" && -z "${YORIK_E2E_REAL_LLM:-}" ]] \
   && models=$(curl -sf -m 3 http://127.0.0.1:8080/v1/models); then
  export YORIK_E2E_REAL_LLM=http://127.0.0.1:8080/v1
  export YORIK_E2E_MODEL=$(printf '%s' "$models" | venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])')
fi
[[ "${FAKE_LLM:-0}" == "1" ]] && unset YORIK_E2E_REAL_LLM

venv/bin/python e2e/household.py up || exit 2

status=0
rm -rf e2e/report && mkdir -p e2e/report
# The journeys judge a freshly seeded household, so they go first; the
# crawler clicks everything (sharing toggles, ticks) and goes last.
if [[ $# -eq 0 ]]; then                       # a full run, not a quick crawl
  venv/bin/python e2e/journeys.py || status=1
  (cd e2e && node ui_journeys.mjs) || status=1
fi
(cd e2e && node crawl.mjs "$@") || status=1
verdict=$(venv/bin/python e2e/summarize.py | tail -1)

[[ "${KEEP:-0}" == "1" ]] || venv/bin/python e2e/household.py down

stamp=$(date +%Y-%m-%d-%H%M)
mkdir -p "e2e/history/$stamp" && cp -r e2e/report/. "e2e/history/$stamp/"
cp e2e/.run/blocked.log "e2e/history/$stamp/" 2>/dev/null || true
ls -1d e2e/history/20*-[0-9][0-9][0-9][0-9] 2>/dev/null | sort | head -n -30 | xargs -r rm -rf   # keep 30 runs

token_file="$HOME/.config/yorik-e2e/token"
if [[ -s "$token_file" ]]; then
  venv/bin/python - "$token_file" "$verdict" "$ROOT/e2e/history/$stamp/README.md" <<'EOF' || echo "bell message not sent"
import json, sys, requests
token = open(sys.argv[1]).read().strip()
verdict, report = sys.argv[2], sys.argv[3]
body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "notify", "arguments": {"title": verdict, "body": f"Report: {report}"}}}
r = requests.post("http://127.0.0.1:8000/mcp", json=body, timeout=20,
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})
print("bell:", r.status_code)
EOF
fi

echo
echo "$verdict — e2e/report/README.md (kept in e2e/history/$stamp)"
[[ -s e2e/.run/blocked.log ]] && echo "The test server tried to reach (and was refused): $(awk '{print $3}' e2e/.run/blocked.log | sort -u | tr '\n' ' ')"
exit $status
