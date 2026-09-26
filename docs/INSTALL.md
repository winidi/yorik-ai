# Installing Yorik

Yorik runs as one Docker stack (`deploy/compose.yaml`), the same on
Linux, Windows and macOS. Each installer below sets up Docker if needed,
starts the stack, and opens Yorik in the browser, where you create your
account. The short version is in the [README](../README.md#install).

> Yorik is in **alpha**. It runs every day in the maintainer's household,
> but you may be among the first to install it elsewhere. Don't make it
> your only copy of your photos yet.

## What you need

| | Minimum | Recommended | Why |
|---|---|---|---|
| RAM | 8 GB | 16 GB | AI model 4–8 GB, photos 2 GB, documents 1 GB, Yorik 0.5 GB |
| Disk | 50 GB free | 100 GB+ | the images (~8 GB), the model (~7 GB), then photos and scans |
| GPU | none | NVIDIA | replies in about a second instead of 5–10 (Windows and Linux) |
| Network | internet during install | | images and the model download once |

**Where it lives matters more than the operating system.** Yorik is a
butler for the family: reminders, mail and WhatsApp want a machine that
is on around the clock. A PC that sleeps at night takes Yorik with it.
Trying it on your PC is fine; for everyday family use, a used mini PC
with the install stick (below) is the setup to aim for.

## Windows 10 / 11

1. From the [latest release](https://github.com/winidi/yorik-ai/releases/latest)
   download `Yorik-Setup-Windows.zip`, unzip it.
2. Double-click `Yorik-Setup.cmd` and allow administrator rights.

It turns on WSL2 and installs Docker Desktop through winget if they're
missing (Windows then needs **one restart**; setup continues by itself
after you sign in again), puts Yorik in `%LOCALAPPDATA%\Yorik`, backups
in `Documents\Yorik Backups`, starts the stack, adds a "Yorik" shortcut
to the desktop and opens the browser. Docker Desktop is free for
personal use and small businesses; its license applies.

Phones at home reach Yorik at `http://<this PC's address>:8000`; Windows
may ask once whether Docker may accept connections on private networks,
say yes.

## macOS

From the release, `Yorik-Setup-Mac.zip`, then double-click
`Yorik Setup.command`. It installs Docker Desktop (Homebrew if present,
otherwise the official download), puts Yorik in
`~/Library/Application Support/Yorik`, backups in `~/Documents/Yorik Backups`.
Docker can't use the Apple GPU; with the [Ollama app](https://ollama.com)
installed, Yorik uses it instead, which is much faster.

Not yet tested on a real Mac. Reports welcome.

## Linux

```bash
git clone https://github.com/winidi/yorik-ai && cd yorik-ai
bash install.sh
```

Ubuntu 24.04+, Debian 12+ or Fedora 39+. It asks nothing except your
`sudo` password: installs Docker Engine (and NVIDIA's container toolkit
if there's a GPU), starts the stack from `deploy/`, and ends with a QR
code for your phone. Flags: `--llm=none` (no model now), `--ask`.
`--classic` installs the pre-Docker way (see the end of this page).

## A mini PC as the family's box

```bash
bash scripts/build-appliance.sh      # → dist/yorik-appliance.iso
```

Write the image to a USB stick (balenaEtcher, Raspberry Pi Imager),
boot the PC from it. **It erases that PC's disk** and installs Ubuntu
Server and Yorik by itself; the screen then shows a Tailscale sign-in
code and, once Yorik runs, a QR code for your phone. At home it also
answers at `http://yorik.local:8000`.

## Access on the go: Tailscale

Yorik never faces the open internet. The stack includes
[Tailscale](https://tailscale.com) (free for up to 6 people). Sign in
once: Yorik → Settings → System → Phones shows a QR code (on the box's
screen too). Then Yorik is at `https://yorik.<your-tailnet>.ts.net`,
photos on `:8443`, documents on `:8444`, with real HTTPS, which browsers
need for the microphone and "Add to Home Screen".

Two switches in the Tailscale admin console:

1. **HTTPS certificates**: [DNS settings](https://login.tailscale.com/admin/dns)
   → MagicDNS and HTTPS on.
2. **Funnel** for the public invite page (optional):
   [Access controls](https://login.tailscale.com/admin/acls) → allow the
   `funnel` node attribute, then `YORIK_TS_SERVE=serve-funnel.json` in
   `.env` and `docker compose up -d tailscale`.

## Getting the family in

Home → "Set up Yorik" → **Invite your family**, or Settings → Users →
**Invite with a QR code**. The person scans it with the phone camera,
picks a name, a colour and a 4-digit PIN, and puts Yorik on the home
screen. Children's accounts get a simpler Yorik.

## Updates

Settings → System → Updates shows when a new version is out, with its
changes, and **Update now** pulls the new images and restarts; your data
stays. Installs follow `stable` (released versions); `YORIK_VERSION` in
`.env` pins a version or picks `edge` (every change on main).

## Where things are

| | |
|---|---|
| `.env` next to `compose.yaml` | settings and generated passwords (keep private) |
| Docker volumes `yorik_*` | database, photos, documents, WhatsApp session, model, Yorik's files |
| Backups folder (`YORIK_BACKUP_DIR`) | encrypted snapshots; Home → "Set up Yorik" → Turn on backups |

Useful commands, in the folder with `compose.yaml`:
`docker compose ps`, `docker compose logs -f yorik`, `docker compose stop`,
`docker compose up -d`.

## Removing Yorik

Linux: `bash scripts/uninstall.sh`. Elsewhere, in the folder with
`compose.yaml`: `docker compose down -v` (removes everything including
the data; the backups folder stays).

## Classic install (development)

`bash install.sh --classic` sets Yorik up directly on the machine:
Python venv, systemd unit, the Supabase stack via `start.sh`, Tailscale
on the host. It's the maintainer's development setup (fast reloads, no
image builds) and what older installs run. `--container` is the classic
setup with Yorik itself in a container. `bash start.sh` starts a classic
checkout by hand.

## When something goes wrong

The installers print what to do under every error. More in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). What Yorik does and doesn't
protect against: [THREAT_MODEL.md](../THREAT_MODEL.md). What touches the
network: [PRIVACY.md](PRIVACY.md).
