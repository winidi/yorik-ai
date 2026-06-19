# Troubleshooting

When something breaks. Each entry follows the same shape:

> **Symptom** — what you see
> **Diagnose** — copy-paste commands to confirm the cause
> **Fix** — what to do about it

If your problem isn't here, please [file a bug report](https://github.com/winidi/yorik-ai/issues/new/choose).
The fixes that go into this file usually come from real bug reports.

## Install / startup

### "Port 8000 already in use" on `bash start.sh`

> **Symptom**: `start.sh` exits with `ERROR: [Errno 98] Address already in use`.
>
> **Diagnose**:
> ```bash
> lsof -i :8000
> # or
> sudo ss -tlnp | grep ':8000'
> ```
>
> **Fix**: Either kill the other process, or change Yorik's port. To change:
> ```bash
> echo "HOMEOS_PORT=8001" >> config.env
> ```
> Then restart. If the leftover process is Yorik itself from a prior session:
> ```bash
> pkill -KILL -f "uvicorn backend.main"
> ```

### "docker: permission denied" mid-install

> **Symptom**: `start.sh` halts at the Docker step with a `permission denied while trying to connect to the Docker daemon socket` message.
>
> **Diagnose**:
> ```bash
> groups | tr ' ' '\n' | grep -w docker
> # empty = your user isn't in the docker group yet
> ```
>
> **Fix**:
> ```bash
> sudo usermod -aG docker $USER
> newgrp docker   # picks up the new group without a logout
> bash start.sh   # retry
> ```

### "ModuleNotFoundError: No module named 'X'" when running yorik CLI

> **Symptom**: `./scripts/yorik backup-verify` (or similar) fails with a missing Python module.
>
> **Diagnose**: the CLI auto-execs through `venv/bin/python3` — verify it exists:
> ```bash
> ls -la venv/bin/python3
> ```
>
> **Fix**: rebuild the venv:
> ```bash
> rm -rf venv/
> bash start.sh
> ```

### `start.sh` hangs at "downloading whisper turbo"

> **Symptom**: the voice-model download takes ages or appears stuck.
>
> **Diagnose**: it's ~1.5 GB and pulls from HuggingFace. Slow connection makes it look frozen. Check disk activity:
> ```bash
> iotop -ao   # in another terminal
> ```
>
> **Fix**: be patient (10+ min on a slow line). If genuinely stuck (zero IO for >5 min), `Ctrl-C` and re-run — `start.sh` is idempotent. To skip whisper entirely and use a smaller model:
> ```bash
> echo "HOMEOS_WHISPER_MODEL=base" >> config.env
> ```

## LLM / chat

### Chat replies are slow (>10 seconds)

> **Symptom**: every chat turn takes 10+ seconds to start streaming.
>
> **Diagnose**: which model are you on?
> ```bash
> curl -s http://localhost:8000/api/health | python3 -m json.tool | grep model
> ```
>
> **Fix**: a 7–9B chat model on CPU is the slow path. Faster options:
> - Smaller model: switch to a 3–4B variant in Settings → LLM
> - GPU: install CUDA + the cuda variant of Ollama or llama.cpp
> - Better quant: prefer a `Q4_K_M` GGUF over `Q8_0` if you're on llama.cpp

### Chat returns "LLM unreachable — service degraded" badge

> **Symptom**: red banner in the Chat app, replies look like canned errors.
>
> **Diagnose**:
> ```bash
> # Whichever URL your Settings → LLM tab shows:
> curl -s http://localhost:11434/api/tags | head     # Ollama
> # or
> curl -s http://localhost:8080/v1/models | head    # llama.cpp / llama-swap
> ```
>
> **Fix**: restart your LLM. With Ollama:
> ```bash
> pkill ollama
> ollama serve &
> ```

### Chat answers in English when I asked in German (or vice versa)

> **Symptom**: language mismatch between question and reply.
>
> **Diagnose**: the agent uses your user-profile language as a default. Check it:
> ```bash
> # via the API
> curl -s -b cookies.txt http://localhost:8000/api/auth/me | python3 -m json.tool
> ```
>
> **Fix**: Settings → People → click your user → Language dropdown.

## Voice

### "Mic permission denied" in the browser

> **Symptom**: clicking the voice FAB shows a permission error.
>
> **Fix**: browsers restrict mic access on insecure (non-HTTPS) origins. Either:
> - Use `localhost` — most browsers allow mic on localhost even over HTTP
> - Put TLS in front of Yorik (Caddy + Tailscale; instructions in INSTALL.md)
> - For Firefox: `about:config` → set `media.devices.insecure.enabled` to true (DEV ONLY)

### Voice transcribes the wrong language

> **Symptom**: spoken German comes back as gibberish English (or vice versa).
>
> **Diagnose**: Whisper auto-detects language. Detection is iffy on short utterances.
>
> **Fix**: pin the language in `config.env`:
> ```bash
> echo "HOMEOS_WHISPER_LANGUAGE=de" >> config.env
> sudo systemctl restart yorik   # or pkill + start.sh
> ```

### TTS voice plays at wrong speed / sounds robotic

> **Symptom**: replies sound chipmunk-fast or weirdly low-pitched.
>
> **Diagnose**: Supertonic-3 voice + locale mismatch. Check which voice is loaded:
> ```bash
> grep TTS /tmp/homeos-api.log | tail -5
> ```
>
> **Fix**: override per-language in `config.env`:
> ```bash
> echo 'HOMEOS_TTS_VOICE_DE=de-female-1' >> config.env
> echo 'HOMEOS_TTS_VOICE_EN=en-male-1' >> config.env
> sudo systemctl restart yorik
> ```

## Calendar

### Travel time shows NULL right after I save an event

> **Symptom**: you save an event with a location, the card renders without the amber 🚗 badge, then 10–60s later the badge appears on its own.
>
> **Diagnose**: this is expected — travel time is computed asynchronously by the maps connector after the event row commits. Check the worker chip on the home screen, or:
> ```bash
> sqlite3 data/family.db "SELECT id, title, location, travel_seconds, travel_computed_at FROM events ORDER BY id DESC LIMIT 5;"
> ```
> A row with `location` set but `travel_seconds` NULL and `travel_computed_at` NULL has not been picked up yet.
>
> **Fix** (if it never arrives): the maps connector is probably down or rate-limited — see the "Connectors → Maps" section below. To re-trigger the compute for a single event:
> ```bash
> curl -X POST http://localhost:8000/api/events/123/recompute-travel -b cookies.txt
> ```

### Travel time is wildly wrong (200 km/h on a city trip)

> **Symptom**: 🚗 badge says "↗ 4 min · leave 13:56" for a 30-km drive.
>
> **Diagnose**: the geocoder picked the wrong "Berliner Straße" (there are ~700 in Germany). Hover the location chip → the resolved coordinates show on hover.
>
> **Fix**: edit the event and add the city to the location field (`Berliner Straße 5, Hannover`). Re-save triggers a fresh geocode + travel compute.

## Compose / Templates

### Preview renders fine but the saved PDF has empty fields

> **Symptom**: the in-app preview shows your recipient address and body, but the downloaded PDF has blank spots where those should be.
>
> **Diagnose**: this almost always means the template has a `{{ variable }}` reference without a matching `{% if variable %}` guard, and the variable was missing at PDF-render time. Check:
> ```bash
> grep -n "{{" backend/templates/<your-template>/template.json
> # any reference without an {% if %} around it is a candidate
> ```
>
> **Fix**: wrap the variable in a guard. Pattern from `letter-of-demand-us`:
> ```
> {% if recipient_address %}<p>{{ recipient_address | safe }}</p>{% endif %}
> ```
> The memory `feedback_verify_template_renders.md` is a permanent reminder: whenever Compose templates / `pdf.py` / `render.py` / TipTap config changes, render the PDF and `pdftotext -layout` it before declaring done.

### "Template variable unknown" red banner in Compose

> **Symptom**: opening a template surfaces a red banner naming a variable that doesn't exist in the template.
>
> **Diagnose**: the template's `preview_args` or `ask_user_for_args` list references a key not used in the body, or the body references a key not declared.
>
> **Fix**: keep `ask_user_for_args` + `default_args` + body variables in sync. The template-lint job in CI (`.github/workflows/lint-templates.yml`) catches this on push.

## Web search / fetch

### "Yorik kept searching but never answered"

> **Symptom**: the chat shows 3+ `web_search` cards in a row with similar queries, then the LLM gives a vague answer or gives up.
>
> **Diagnose**: the agent loop bailed out at the search-iteration cap (default 4 per turn). Logs will say `web_search: iteration cap reached`. Common causes:
> - The query is too specific for any one page (e.g. "Stadtbad Hannover ermäßigter Eintritt für 3 Kinder und 1 Erwachsenen Familienkarte 2026")
> - The redacted query lost the discriminating phrase (a multi-word name that was the whole point of the search)
> - The site is JS-only and Trafilatura returned nothing useful
>
> **Fix**: rephrase to a single concrete question Yorik can answer in one search ("Was kostet die Familienkarte im Stadtbad Hannover?"). For JS-heavy sites, swap to a different extract provider (Tavily / Firecrawl planned for v0.3.0).

### `web_extract` refuses with "private or local address"

> **Symptom**: trying to extract from a URL returns "refused: private or local address".
>
> **Diagnose**: this is the SSRF guard at `backend/agent/providers/web_search/trafilatura.py:_is_private_or_local()`. Refused targets: `localhost`, `127.0.0.0/8`, `10.0.0.0/8`, `192.168.0.0/16`, `169.254.0.0/16` (AWS metadata), `0.0.0.0`, `file://`, anything non-http(s). The check re-resolves DNS at fetch time, so a hostname that points to a public IP at first lookup but flips to a private one (DNS rebinding) is also refused.
>
> **Fix**: this is intentional. To intentionally let Yorik read an internal page, do it via the user (paste the text into chat) rather than disabling the guard.

### Web search query log shows my name even though PII redaction is on

> **Symptom**: in Settings → Privacy → "What did Yorik look up?", you see your name in a query.
>
> **Diagnose**: `redact_pii()` only strips **multi-word phrases** (full name, street address, multi-word contact display name). A single first name like "Anna" is too generic to redact safely — see `docs/PRIVACY.md` § "Web search and PII redaction".
>
> **Fix** (if you want stricter redaction): edit `backend/skills/_web_helpers.py:redact_pii()` to add your own rules. Single-word redaction will break legitimate queries ("Steuerberater Anna in Hannover" loses meaning), so the default trades a small leak for usefulness — adjust per your threat model.

## Connectors

### Maps badge red ("not reachable")

> **Symptom**: Settings → System status shows the Maps connector red. New event locations don't geocode; travel time never appears.
>
> **Diagnose**:
> ```bash
> curl -s -o /dev/null -w "%{http_code}\n" https://nominatim.openstreetmap.org/status
> # 200 = OSM up; anything else = upstream down
> tail -200 /tmp/homeos-api.log | grep -i "nominatim\|osrm\|overpass"
> # look for rate-limit (429) or User-Agent-rejected (403) lines
> ```
>
> **Common causes**:
> - You hit Nominatim's [usage policy](https://operations.osmfoundation.org/policies/nominatim/) — max 1 req/s per IP, no bulk lookups. Yorik throttles to 1.2s but a backfill job can still bump it.
> - Your User-Agent string was rejected. The connector defaults to `Yorik/<version> (self-hosted)`. Override via `HOMEOS_MAPS_USER_AGENT=...` in `config.env`.
>
> **Fix**: wait an hour (rate limits reset) or point at a self-hosted Nominatim mirror:
> ```bash
> echo "HOMEOS_NOMINATIM_BASE=https://your-mirror.example/nominatim" >> config.env
> ```

## SSE streaming

### Chat hangs mid-stream, then jumps to the final answer

> **Symptom**: tokens stream for a few seconds, the spinner stalls for 10–30s, then the full reply appears at once.
>
> **Diagnose**: a reverse proxy (nginx, Cloudflare Tunnel, Caddy without the right config) is buffering the SSE stream. To confirm, bypass the proxy:
> ```bash
> curl -N -b cookies.txt http://localhost:8000/api/ask/stream \
>   -H 'content-type: application/json' \
>   -d '{"message":"hello","role":"admin"}'
> # tokens should arrive one-per-line as they're generated
> ```
> If the direct call streams correctly but the proxied URL stalls — it's the proxy.
>
> **Fix**:
> - **nginx**: add `proxy_buffering off;` + `proxy_cache off;` in the `/api/ask/stream` location block
> - **Caddy**: SSE works out of the box with `reverse_proxy`; no extra config needed
> - **Cloudflare**: free-tier proxies buffer SSE. Use a Tunnel (cloudflared) instead of an orange-cloud proxy.

### `/api/ask/stream` returns 502 after exactly 30 seconds

> **Symptom**: long-running agent turns (multi-step tool calls) get killed at the 30s mark.
>
> **Diagnose**: your reverse proxy has a 30s read timeout (nginx default for `proxy_read_timeout`).
>
> **Fix**: bump the timeout for the streaming endpoint to 5+ minutes:
> ```nginx
> location /api/ask/stream {
>     proxy_pass http://localhost:8000;
>     proxy_buffering off;
>     proxy_read_timeout 300s;
>     proxy_send_timeout 300s;
> }
> ```

## Email / IMAP

### "IMAP login failed: AUTHENTICATIONFAILED"

> **Symptom**: Email account shows red in Settings → Connectors after adding.
>
> **Common cause**: provider blocks app passwords for Less Secure Apps. Gmail, Outlook, and iCloud all require dedicated app passwords now.
>
> **Fix**:
> - **Gmail**: enable 2FA, then generate an [App Password](https://myaccount.google.com/apppasswords)
> - **iCloud**: [App-specific password](https://support.apple.com/en-us/HT204397) (requires 2FA)
> - **Outlook/Hotmail**: [App password](https://support.microsoft.com/en-us/account-billing/manage-app-passwords-for-two-step-verification-d6dc8c6d-4bf7-4851-ad95-6d07799387e9)

### IMAP IDLE works but new mail doesn't appear

> **Symptom**: home-screen Background workers chip says "Email account 1 idling" (green) but no new emails show up in the Email app.
>
> **Diagnose**:
> ```bash
> # Force-sync via the API
> curl -X POST http://localhost:8000/api/email/accounts/1/sync \
>   -b cookies.txt
> ```
>
> **Fix**: check the worker log for the actual IMAP error:
> ```bash
> journalctl -u yorik -n 100 | grep email
> ```

## Paperless

### "Paperless not reachable" badge on home

> **Symptom**: Paperless badge red in Settings → System status.
>
> **Diagnose**:
> ```bash
> curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8010/api/
> # expect 401 (means it's UP, just rejecting unauthed)
> ```
>
> **Fix**:
> - If you get connection refused: `docker compose up -d paperless-web`
> - If you get 401 but Yorik still says "not reachable": the API token is missing. Settings → Connectors → Paperless → paste your API token. Generate one in Paperless UI: Profile → API Tokens → Create.

### Documents you added in Paperless don't show up in Yorik

> **Symptom**: you uploaded to Paperless directly, but `/r/documents` doesn't list them.
>
> **Diagnose**: check the reconciler chip on the home screen — if it says "X missing", the auto-reconciler hasn't run yet.
>
> **Fix**: the reconciler runs at startup + every 6 hours. To trigger now:
> ```bash
> sudo systemctl restart yorik   # the simplest force-trigger
> # or
> curl -X POST http://localhost:8000/api/paperless/reindex-all -b cookies.txt
> ```

## Immich

### Photos search returns nothing for a person who's clearly labelled

> **Symptom**: "show me photos of Sara" → "no photos found", but Sara is named in Immich → People.
>
> **Diagnose**: face recognition is still running.
> ```bash
> docker logs homeos-immich-ml --tail 50
> # look for "face detection" lines
> ```
>
> **Fix**: be patient — face recognition on a large library takes hours on CPU. Settings → Immich → Trigger reprocess if it appears stuck.

### Immich uses 100% GPU even when idle

> **Symptom**: nvidia-smi shows constant 100% on the Immich ML container.
>
> **Fix**: face / object / search ML jobs are catching up on a large initial import. Will settle once done. To pause: `docker compose stop immich-machine-learning`.

## Backup / restore

### Backup verify fails

> **Symptom**: `yorik backup-verify` prints any ✗ lines or red `FAILED` summary.
>
> **Per-check fixes**:
> - `decrypt`: wrong passphrase. Try again — passphrases are case-sensitive.
> - `extract`: snapshot file is corrupt. Pull a different snapshot via `yorik backup-list`.
> - `manifest` missing: snapshot wasn't built by Yorik or pre-dates v0.1.0-alpha.
> - `sqlite_*_integrity` failed: the snapshot ran while a write was in flight. Re-take the backup; SQLite's `VACUUM INTO` should usually handle this.

### Lost the backup passphrase

> **Symptom**: `yorik backup-verify` prints "wrong passphrase or corrupt snapshot" with the correct file.
>
> **Fix**: there isn't one. The passphrase is the encryption key — without it, snapshots are unrecoverable. Set a new passphrase in Settings → Backup and run a fresh backup. Going forward, write the passphrase down somewhere outside the box.

## Upgrade

### `yorik upgrade` refuses with "working tree has uncommitted changes"

> **Symptom**: upgrade aborts before pulling.
>
> **Fix**: this is intentional — auto-pulling over local edits would destroy them. Either:
> ```bash
> git status                # see what's modified
> git stash                 # park the changes
> ./scripts/yorik upgrade
> git stash pop             # restore them
> ```
> Or commit + push your edits first (then they'll be preserved on the next pull anyway).

### Upgrade pulled the code but `pip install` failed

> **Symptom**: `yorik upgrade` printed `pip install failed` and exited.
>
> **State**: your code is on the new version but Python deps are still on the old. The backend probably won't start.
>
> **Fix**: run pip manually with verbose output to see what broke:
> ```bash
> ./venv/bin/pip install -r backend/requirements.txt
> ```
> Common causes: missing system lib (e.g. libffi-dev), Python version mismatch, network blip.

## Systemd service

### `yorik service install` says "already installed"

> **Fix**:
> ```bash
> ./scripts/yorik service uninstall   # clean slate
> ./scripts/yorik service install     # reinstall
> ```

### Service starts but the dashboard returns 502 / connection refused

> **Diagnose**:
> ```bash
> sudo systemctl status yorik
> journalctl -u yorik -n 100 --no-pager
> ```
>
> Common: the LLM endpoint isn't ready when systemd brings up Yorik. The unit has `After=network-online.target` but your LLM might run as a separate user/service. Wait 30s and refresh; if persistent, set the LLM to also auto-start (e.g. `systemctl --user enable ollama`).

## Frontend / UI

### Dashboard shows a blank page after upgrade

> **Symptom**: `localhost:8000` loads HTML but nothing renders.
>
> **Diagnose**: open DevTools (F12) → Console tab. Look for `404` on a JS bundle.
>
> **Fix**: the frontend bundle hash changed in the upgrade but your browser cached the old `index.html`. Hard-refresh:
> - Linux/Windows: `Ctrl+Shift+R`
> - Mac: `Cmd+Shift+R`
>
> If that doesn't help: `npm run build` didn't run during upgrade. Rebuild:
> ```bash
> cd frontend-react && npm run build
> sudo systemctl restart yorik
> ```

### A specific app shows "Something went wrong" with no detail

> **Diagnose**: DevTools → Console for the JS error; DevTools → Network for the API call that failed.
>
> If the API call is 500: `journalctl -u yorik -n 50` (or `tail -50 /tmp/homeos-api.log`) shows the Python traceback.
>
> **File a bug** with both — that's the most actionable report we can get.

## Filing a useful bug report

When in doubt, [open an issue](https://github.com/winidi/yorik-ai/issues/new/choose). The most useful reports include:

1. **Your setup**:
   - OS (`lsb_release -a`)
   - Yorik commit (`git rev-parse --short HEAD`)
   - LLM provider + model (`curl http://localhost:8000/api/health`)
2. **Exact action that broke it** — "I clicked X, expected Y, got Z"
3. **Backend log tail**:
   ```bash
   journalctl -u yorik -n 50 --no-pager > yorik-log.txt
   # or
   tail -100 /tmp/homeos-api.log > yorik-log.txt
   ```
4. **Browser console output** if it's UI — F12 → Console → screenshot or copy
5. **Network response** if a specific API call broke — F12 → Network → click the red request → "Copy as cURL"

The bug template in the issue tracker prompts for all of the above; just paste the outputs.
