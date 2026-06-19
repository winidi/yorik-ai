# Installing Yorik

This is the full walk-through for setting up Yorik on a fresh Ubuntu
or Debian machine. The TL;DR in [README.md](../README.md) gets you
to a running server in two commands; this doc explains what each step
does, what can go wrong, and how to recover if it does.

> **Read this first**: Yorik is in **alpha**. It runs end-to-end on
> the maintainer's box, but you may be the first non-maintainer to
> install it on yours. Set expectations accordingly — don't install
> it as your only photo backup. Have an existing Paperless or Immich?
> Yorik will use them as-is; you don't need to migrate.

## What you'll end up with

- A web app at `http://localhost:8000` (your dashboard)
- A local LLM endpoint you bring (Ollama recommended for first install)
- Optional bundled stack: Immich, Paperless, WhatsApp bridge — brought
  up via Docker Compose only if the corresponding port is free.
  n8n is supported as a BYO workflow runtime (see
  [CONNECTORS.md](CONNECTORS.md)) — Yorik proxies and integrates with
  your own n8n but does not bundle it (Sustainable Use License is not
  OSI-approved open source).
- An encrypted SQLite database under `data/family.db`
- Optional: a `yorik.service` systemd unit that auto-starts on boot

Total install time: **5–15 minutes** depending on your network speed
and whether you already have Docker installed.

## Hardware requirements

| Resource | Minimum | Recommended | Why |
|---|---|---|---|
| RAM | 8 GB | **16 GB** | LLM (4–8 GB) + Immich ML (2 GB) + Paperless (1 GB) + Yorik (0.5 GB) |
| Disk | 30 GB free | 100 GB+ | Photo library + document scans grow fast |
| CPU | x86_64 (any modern) | 6+ cores | Whisper STT + Immich face detection benefit |
| GPU | none required | NVIDIA with CUDA | Immich face detection + larger LLM inference |
| OS | Ubuntu 22.04 / 24.04, Debian 12 | Same | Other distros work but you'll handle Docker setup yourself |

Yorik runs on a Raspberry Pi 5 with 8 GB if you skip Immich and use a
small (1–2B) chat model, but the experience is sluggish. A used Intel
NUC or a Beelink mini-PC is the sweet spot.

## Step 1: System prerequisites

```bash
sudo apt update
sudo apt install -y \
    git curl ca-certificates \
    python3 python3-venv python3-pip \
    ffmpeg sqlite3
```

`ffmpeg` is required for Whisper voice transcription. `sqlite3` is
optional but useful for poking at the database with `sqlite3 data/family.db`.

### Docker (for the optional bundled services)

If you don't already have Docker:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker  # or log out + back in
```

Verify: `docker compose version` should print `Docker Compose version v2.x.x`.

**Skip this** if you don't want Immich / Paperless / WhatsApp. (n8n is BYO — install it yourself if you want it; Yorik never bundles it.)
Yorik works without Docker — you just won't get the bundled apps.

## Step 2: An LLM endpoint (bring your own)

Yorik is a **client** to any OpenAI-compatible local LLM. Pick one:

**Tested with** Qwen 3.5 9B (standard and MTP variants). Any tool-calling
model in the 7–9B class works; smaller (3–4B) works for chat but
struggles with tool selection.

### Option A — Ollama (easiest, recommended for first install)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull <your-chat-model>       # see "Tested with" above
ollama pull nomic-embed-text        # embeddings model (~250 MB)
ollama serve &                      # binds 127.0.0.1:11434
```

Verify: `curl -s http://localhost:11434/api/tags | head` should list
the models you pulled.

### Option B — llama.cpp with a GGUF model

Best CPU-only performance. More setup; pick a GGUF from HuggingFace
that matches a tested model and run `llama-server` against it.

### Option C — LM Studio (GUI, all platforms)

Install [LM Studio](https://lmstudio.ai), download a model in its UI,
enable "Local Server" mode. It exposes the OpenAI-compatible API on
`:1234`.

### Option D — Already running something else?

Yorik talks to anything that speaks OpenAI's `/v1/chat/completions`
contract: vLLM, llama-swap, TGI, even a cloud endpoint
(at the cost of privacy — Yorik will warn you in the LLM settings).
Configure the base URL during onboarding.

## Step 3: Clone and run

```bash
git clone https://github.com/winidi/yorik-ai
cd yorik-ai
bash start.sh
```

`start.sh` is **idempotent** — re-running it is safe and only does work
that hasn't been done. On the first run it:

1. Detects your timezone + locale for the bundled Docker services
2. Probes your LLM endpoint
3. Creates the Python venv at `venv/` and installs dependencies
4. Downloads voice models (Whisper turbo, Supertonic-3 TTS, SpeechBrain
   speaker ID) — **~500 MB total**, one-time
5. Initialises the SQLite database at `data/family.db`
6. Brings up the optional Docker stack with auto-detected profiles
   (skips any service whose port is already taken on the host)
7. Starts the FastAPI backend at `http://localhost:8000`

You'll see colored `[PHASE]` headers as each step runs. The whole thing
prints a "Yorik / HomeOS running" banner when complete with the URLs to
open.

### Optional: install the systemd service

At the end of the first successful start, you'll see:

```
─────────────────────────────────────────────────────────
  Optional: install a systemd service?

  Without it, Yorik runs only while this shell stays open. If
  the box reboots, or Python crashes at 3am, you have to start
  it again by hand.

  With it (recommended for any always-on box):
    • auto-starts on boot
    • auto-restarts on crash (5s delay, 5-strike backoff)
    • logs to the journal (journalctl -u yorik -f)
    • sudo systemctl restart yorik  after `yorik upgrade`
─────────────────────────────────────────────────────────
Install yorik.service now? [y/N]
```

Pick `y` if this is a machine that should stay up. You can install or
remove the service later with `./scripts/yorik service install` or
`./scripts/yorik service uninstall`.

## Step 4: Open the dashboard

```bash
xdg-open http://localhost:8000     # or just open in your browser
```

Yorik shows a **first-run wizard**:

1. **Welcome** — quick orientation
2. **Local AI** — verifies your LLM endpoint is reachable. If the
   model name shown isn't what you expect, you can change it in
   Settings → LLM after onboarding.
3. **Email** — connect an IMAP account, or skip. You can come back
   to this in Settings → Connectors → Email.
4. **Try saying…** — three example prompts to test the chat. Each
   one drops you into the Chat app with the prompt pre-filled.

After the wizard, you're on the home screen — a 4-up app grid with
the system status row underneath.

## Step 5: Verify your install

Run through a quick checklist:

```bash
# 1. Backend health
curl -s http://localhost:8000/api/health | python3 -m json.tool

# 2. Tests (if you cloned for development)
./venv/bin/python -m pytest tests/ -q

# 3. Schema migrations status
./scripts/yorik db status

# 4. Service status (if you installed the systemd unit)
./scripts/yorik service status
```

In the browser:

- **Chat**: ask "what's on my calendar today" — should respond, not error
- **Calendar**: drag-create an event, refresh — it persists
- **Settings → Connectors**: each connector you configured shows green
- **Settings → System status**: LLM badge green, Backup badge configured (after Step 6)
- **Bell icon** (top-right): a couple of notifications visible

## Step 6: Configure backups (do this before you have anything to lose)

Yorik backs up the entire `data/` directory (SQLite + documents +
voice embeddings + credentials) into an age-encrypted tarball.

1. Open **Settings → Backup**
2. Set a **passphrase** (≥8 characters). Write it down somewhere safe
   that isn't this machine. Without it your encrypted backups are
   useless.
3. Pick a **schedule** (e.g. `02:00` daily)
4. Optionally set a **target path** to an external drive (`/media/usb/yorik-backups`)
5. Click "Run backup now" — first backup takes 10–30 seconds

Verify a backup is restorable:

```bash
./scripts/yorik backup-verify
# Will prompt for the passphrase you just set
```

Should print 8+ green checkmarks. If any fail, see
[TROUBLESHOOTING.md → Backup verify fails](TROUBLESHOOTING.md#backup-verify-fails).

## Step 7: Day-2 operations

| Want to… | Do this |
|---|---|
| Update Yorik | `./scripts/yorik upgrade` (commits → pulls → installs deps → restarts) |
| Tail logs | `journalctl -u yorik -f` (with systemd) or `tail -f /tmp/homeos-api.log` |
| Stop the service | `sudo systemctl stop yorik` or `kill $(cat /tmp/homeos-api.pid)` |
| Add a user | Settings → People → New user |
| Add a community template | Compose → "Browse community templates" → Install |
| Run a backup now | Settings → Backup → "Run backup now" |
| See what schema you're on | `./scripts/yorik db status` |

## Where data lives

| Path | What |
|---|---|
| `data/family.db` | Main SQLite database (events, tasks, bills, conversations, sessions, …) |
| `data/documents.db` | Vector index (sqlite-vec) for documents + photos |
| `data/documents/` | Original uploaded document files |
| `data/.credential_key` | Fernet key encrypting external API tokens. **Back this up.** Without it, encrypted creds in `family.db` are useless. |
| `data/backups/` | Age-encrypted backup snapshots |
| `data/immich/`, `data/paperless/` | The bundled services' data (only if you opted in) |
| `~/.n8n/` | If you BYO n8n: its data (wherever you installed it). Yorik doesn't manage this. |
| `briefings/` | Daily briefing templates (mostly stock) |
| `templates/` | Compose templates installed from the community catalogue |
| `venv/` | Python virtualenv. Disposable — `start.sh` recreates if missing. |
| `frontend-react/dist/` | Built React bundle. Disposable — `npm run build` regenerates. |

**Backup advice**: everything you care about is in `data/`. A
`tar -czf yorik-data.tar.gz data/` is a usable manual backup if you
don't want to use the built-in flow. The encrypted built-in flow
is recommended because it's automated and the snapshots are safe to
push to a cloud drive.

## Common variations

**On a Raspberry Pi 5**: skip Immich (heavy ML), use a small (1–2B)
chat model, Paperless works but ARM Tesseract is slow. n8n if you
want it can run on a separate machine and Yorik will proxy it. Add
`HOMEOS_DISABLE_IMMICH=1` to `config.env` before first run.

**Default bind is LAN-accessible.** `bash start.sh` listens on
`0.0.0.0:8000` — reachable from any device on the same LAN. This
matches how most self-hosters actually use Yorik (phone, tablet,
other laptop). The admin login is bcrypt over plain HTTP — fine on
a trusted home LAN, not fine on the open internet.

**Restricting to localhost-only**: rerun with
`YORIK_BIND=127.0.0.1 bash start.sh`. Right choice for a laptop you
carry around between networks, or when you're SSH-tunnelling in.

**Exposing beyond the LAN**: don't, not directly. Put Tailscale or a
reverse proxy with TLS (Caddy is easy) in front. The bare admin
login is bcrypt over plain HTTP — fine for behind a tunnel, not fine
on the open internet.

**Behind Tailscale**: works out of the box with the default bind.
See [THREAT_MODEL.md](../THREAT_MODEL.md) for the honest scope.

**On a server you SSH into**: install the systemd service so it
survives logout. Behind Tailscale, the default bind is what you want.
For pure SSH-tunnel access, use `YORIK_BIND=127.0.0.1` plus
`ssh -L 8000:localhost:8000 your-server`.

**Migrating from an existing Yorik install**: copy `data/` over,
then run `bash start.sh` on the new box. Migrations run automatically
to bring the schema up to current; the credential key inside `data/`
means external API tokens still work.

## Next steps

- Read [TROUBLESHOOTING.md](TROUBLESHOOTING.md) before something
  breaks — it's faster to know the playbook than to discover it under
  pressure.
- Read [THREAT_MODEL.md](../THREAT_MODEL.md) so you understand what
  Yorik does and doesn't protect against.
- Read [PRIVACY.md](PRIVACY.md) for what touches the network, what
  stays local, and your GDPR rights in the self-hosted scenario.
- File issues that helped you with [the bug template](https://github.com/winidi/yorik-ai/issues/new/choose).
- Build something yourself: a new connector ([CONNECTORS.md](CONNECTORS.md))
  or a Compose template (the [yorik-community repo](https://github.com/winidi/yorik-community)
  takes PRs).
