---
title: Troubleshooting — common errors + fixes
summary: First-aid for the most common things that go wrong. Logs path, restart sequence, where to file issues.
---

# Troubleshooting — common errors + fixes

First place to look when something's off: `data/logs/yorik.log` (JSON per line, secret-redacted, rotated at 10 MB).

Live tail with the most useful fields:
```bash
tail -f data/logs/yorik.log | jq -c '{ts, level, logger, msg, corr, skill, upstream}'
```

Every chat turn carries a correlation ID (`corr`). When something breaks, grab the ID from the chat UI's error toast and grep: `grep '"corr":"abc123"' data/logs/yorik.log | jq .`.

## "I'll figure something out" failures

### Chat is silent / spins forever

Almost always the LLM. Check Settings → LLM:

- Is the endpoint reachable? Click **Test**. Red = LLM container down or URL wrong.
- Is the model name the actual served model? Some endpoints rename quietly.
- If you swapped LLM recently and the UI says "live-reload": that's the next-chat-turn promise. First fresh turn picks up the change.

If the LLM is fine but chat still hangs: `tail -f data/logs/yorik.log` and look for stack traces around the `corr` ID of your message.

### "Failed to fetch" / 401 / 403 in the browser

Session expired. Click sign-out → sign in again. If that doesn't fix it: cookies → clear `yorik_session` for `localhost`.

### Calendar / Tasks / Contacts app shows blank

Likely a backend startup error. The shell renders even when the API is broken because it caches the last successful state. Restart Yorik and check the startup log for an exception.

### Paperless / Immich icons yellow

The container couldn't be reached. `docker ps` to see if they're running. `docker logs yorik-paperless-web` / `yorik-immich-server` for what's wrong. Common cause: low memory — Immich's machine-learning container alone wants ~4 GB.

### Voice button does nothing

Browser denied microphone access. Re-grant it via the browser's URL-bar permission icon. Refresh.

### Invoice PDF missing the ZUGFeRD XML

Extension not installed OR Yorik wasn't restarted after install. Settings → Extensions: status should be **active**, not **installed, restart pending**. Restart and try again.

### "Couldn't reach the community catalogue"

The yorik-community repo's catalogue.json — either offline or your `YORIK_COMMUNITY_TEMPLATES_URL` points at a wrong path. Default is `https://raw.githubusercontent.com/winidi/yorik-community/main/catalogue.json`. Check that URL in your browser; if it loads there, restart Yorik to clear the catalog cache.

## Restart sequence

Soft restart (picks up code edits, env changes, extension installs):
```bash
bash scripts/restart-uvicorn.sh
```
Doesn't touch the bundled containers (Paperless / Immich / WhatsApp bridge).

Hard restart (everything):
```bash
docker compose down && bash start.sh
```
Use when a bundled service is wedged or you suspect a corrupted state.

## Filing an issue

When you hit something Yorik can't fix itself:

1. In chat, click the message → **debug bundle**. Generates a redacted JSON with the tool trace, agent iteration log, env state. Privacy-preserving (no real names, no document content).
2. Open an issue at `github.com/winidi/yorik-ai/issues` with the bundle attached + what you were trying to do.
3. Security issues — never on the public tracker. Email the address in `SECURITY.md`.

Bug reports with a debug bundle get fixed about 5× faster than ones without.
