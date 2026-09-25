# Installing Yorik

One command sets up everything on a fresh Linux machine and ends with a
QR code you scan with your phone to finish in the browser. This page
explains what that command does, what it needs, and what to do when a
step can't finish on its own. The short version is in the
[README](../README.md#install).

> Yorik is in **alpha**. It runs every day in the maintainer's household,
> but you may be among the first to install it elsewhere. Don't make it
> your only copy of your photos yet. Already run Paperless or Immich?
> Yorik uses them as they are.

## What you need

| | Minimum | Recommended | Why |
|---|---|---|---|
| OS | Ubuntu 24.04, Debian 12, Fedora 39 (or Pop!_OS / Mint on 24.04) | Ubuntu 24.04 | the installer stops on anything older |
| RAM | 8 GB | 16 GB | AI model 4–8 GB, photos 2 GB, documents 1 GB, Yorik 0.5 GB |
| Disk | 50 GB free | 100 GB+ | photos and scans grow fast |
| GPU | none | NVIDIA | replies in about a second instead of 5–10 |
| Network | internet during install | | Docker images and the AI model (~7 GB) |
| Ports free | 8000, 5434, 6544, 8453, 2283, 8010, 3015 | | the installer names the program in the way if one is taken |

A non-root user with `sudo`. Windows: see [WINDOWS.md](WINDOWS.md)
(WSL2). macOS isn't supported by the installer.

## Install

```bash
git clone https://github.com/winidi/yorik-ai && cd yorik-ai
bash install.sh
```

It asks nothing except, if needed, your `sudo` password and, once, a
Tailscale sign-in (a QR code in the terminal). In order:

1. **Checks**: OS, RAM, disk, free ports, network, clock.
2. **Packages**: git, curl, Python, ffmpeg, jq, qrencode; Docker if missing.
3. **AI model**:
   - one already running on a common local port → Yorik uses it;
   - else an NVIDIA GPU → llama.cpp in Docker with Qwen 3.5 9B (MTP,
     UD-Q5_K_XL) and the vision projector; the installer adds NVIDIA's
     container toolkit if it's missing;
   - else → Ollama with `robit/qwen3.5-9b-r7-research-vision:q4km` on
     the CPU (5–10 s per reply).
   Model downloads continue where they stopped if you re-run.
4. **Yorik**: config, Python environment, voice models, the database
   (Supabase), photos (Immich), documents (Paperless), then Yorik itself.
5. **Autostart** at boot (systemd).
6. **Tailscale**, so phones reach Yorik over HTTPS at home and on the go
   (next section).
7. **A QR code**: scan it with your phone to create your account there.

Re-running `bash install.sh` is safe: finished steps are skipped.

### Flags

| Flag | Effect |
|---|---|
| `--ask` | ask before the big steps (the old interactive mode) |
| `--llm=auto` | default, as described above |
| `--llm=ollama` / `--llm=cuda` / `--llm=existing` / `--llm=none` | force one path |
| `--llm=remote=http://10.0.0.5:8080/v1` | a model on another machine (probed first) |
| `--no-autostart` | no systemd unit |
| `--no-tailscale` | home Wi-Fi only, no Tailscale |
| `--dir=PATH` | install somewhere other than this clone / `~/yorik` |

## Tailscale: phones at home and on the go

Yorik never faces the open internet. Phones reach it through
[Tailscale](https://tailscale.com), a free private network (the Personal
plan covers 6 people). The installer installs it, shows a QR code to
sign this machine in to your Tailscale account, and publishes:

| Address | What |
|---|---|
| `https://<machine>.<tailnet>.ts.net` | Yorik |
| `…:8443` | photos (for the Immich phone app) |
| `…:8444` | documents |
| `…:10000` | the public join page for invites (a static page, no data) |

HTTPS matters: browsers only allow the microphone and "Add to Home
Screen" on secure pages.

Two switches in the Tailscale admin console the installer can't flip for
you. It tells you when one is missing; set them and re-run the installer:

1. **HTTPS certificates**: [DNS settings](https://login.tailscale.com/admin/dns)
   → enable MagicDNS and HTTPS.
2. **Funnel** for the join page: [Access controls](https://login.tailscale.com/admin/acls)
   → allow the `funnel` node attribute for this machine. Without it,
   invites still work for phones that already have Tailscale.

Optional, in Yorik → Settings → System → Phones: an OAuth client with
write access to devices. Then every invite also carries a link that
shares only this machine with the new person, so they never see your
other devices.

## Getting the family in

In Yorik: Home → "Set up Yorik" → **Invite your family**, or Settings →
Users → **Invite with a QR code**. The person scans the code with the
phone camera, gets Tailscale if they don't have it yet, picks a name,
a colour and a 4-digit PIN, and puts Yorik on the home screen. No
password, no email needed. Children's accounts get a simpler Yorik.

## After the install

| Want to… | Do this |
|---|---|
| Update | Settings → System → **Update now** when a new version is ready; or `./scripts/yorik upgrade` |
| Turn on backups | Home → "Set up Yorik" → Turn on backups (USB disk + printed recovery sheet) |
| Tail logs | `journalctl -u yorik -f` |
| Stop / start | `sudo systemctl stop yorik` / `start` |
| Check the schema | `./scripts/yorik db status` |
| Remove Yorik | `bash scripts/uninstall.sh` (see below) |

Quick health check: `curl -fsS http://localhost:8000/api/health`.

## Removing Yorik

`bash scripts/uninstall.sh` stops everything and deletes Yorik's
containers, data, systemd units, the AI model it installed and its
Tailscale addresses. It asks you to type `yes` first. Tailscale itself,
Docker and Ollama stay installed (other programs may use them).

## Where data lives

| Path | What |
|---|---|
| `infra/supabase/docker/volumes/db/` | the database: events, tasks, contacts, mail, chats, sessions |
| `data/documents/` | uploaded document files |
| `data/.credential_key` | the key for stored passwords and tokens. **Back it up**; the built-in backup includes it |
| `data/backups/` | encrypted backup snapshots (default target) |
| `data/immich/`, `data/paperless/` | photos and documents |
| `models/` | the AI model files (llama.cpp path) |
| `venv/`, `frontend-react/dist/` | rebuildable |

## Variations

**Manual start without the installer** (development): `bash start.sh`
starts the stack in this clone; `YORIK_BIND=127.0.0.1 bash start.sh`
keeps it on this machine only.

**A model you already run** (LM Studio, vLLM, llama-swap, another box):
`--llm=existing` or `--llm=remote=URL`, or later Settings → LLM → Scan now.

**Raspberry Pi 5 (8 GB)**: `HOMEOS_DISABLE_IMMICH=1` in `config.env`
before the first run and a small (1–2B) model. It works, slowly.

**Moving to a new machine**: install there, then restore a backup
(`docs/RESTORE.md`) or copy `data/` and the database volume over;
migrations bring the schema up to date.

## When something goes wrong

The installer prints a `fix:` line under every error. More in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). What Yorik does and doesn't
protect against: [THREAT_MODEL.md](../THREAT_MODEL.md). What touches the
network: [PRIVACY.md](PRIVACY.md).
