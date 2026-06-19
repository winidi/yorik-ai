---
title: Remote access via Tailscale
nav_app: settings
summary: Set up Tailscale to access Yorik from your phone, laptop, or any other device. No port-forwarding, no DDNS, no public exposure.
---

# Remote access via Tailscale

Yorik runs on your home / office machine. By default it's only reachable from the same Wi-Fi. Tailscale creates a private encrypted mesh between your devices so you can reach Yorik from your phone, a laptop while traveling, etc. — without opening any router ports or exposing anything publicly.

This is what makes Yorik usable as a real personal assistant: type a quick task into the chat from your phone on the train, get the answer back, etc.

## Why Tailscale (vs alternatives)

- **Zero config**: no port forwarding, no DDNS, no firewall fiddling.
- **End-to-end encrypted**: WireGuard under the hood. Even Tailscale Inc can't see your traffic.
- **Free for personal use**: up to 100 devices on the free tier. Family of 4 with 2 phones + 2 laptops each = 8 devices, well within.
- **Reachable by hostname**: instead of remembering an IP, your Yorik machine becomes e.g. `yorik-home.<your-tailnet>.ts.net`.

Yorik doesn't bundle or manage Tailscale — you install it yourself. We just document the recommended setup.

## Setup — 5 minutes

### 1. Install Tailscale on the Yorik machine

**Linux** (typical Yorik host):
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

A login URL prints. Open it in any browser, sign in (Google / Microsoft / GitHub / email).

**macOS**: Tailscale app from the Mac App Store. Sign in.

**Windows**: Download installer from tailscale.com.

### 2. Find the machine's Tailscale name

```bash
tailscale status
```

Output includes a line like `100.x.y.z   yorik-home   yourname@   linux   -`. That `yorik-home` is the hostname you'll use everywhere. Want a different name? `sudo tailscale up --hostname=yorik` to set it.

### 3. Install Tailscale on your phone

Tailscale app from the Play Store / App Store. Sign in with the same account. The phone is now on your tailnet.

### 4. Access Yorik from the phone

Open the phone's browser, go to `http://yorik-home:8000` (or whatever your hostname is + Yorik's port). Login as usual.

That's it. Yorik works exactly the same as on the local network — chat, voice, calendar, everything.

## Best with: MagicDNS

Tailscale → Admin console → DNS → toggle **MagicDNS** on. This lets you use the short hostname (`yorik-home`) instead of the full `yorik-home.tailABCD.ts.net` form. Saves typing on the phone.

## Best with: HTTPS via tailnet certs

If you want `https://` instead of `http://`:

1. Admin console → DNS → enable **HTTPS certificates** for your tailnet.
2. On the Yorik machine: `sudo tailscale cert yorik-home.tailABCD.ts.net`. Cert + key drop into the current directory.
3. Configure Yorik's uvicorn to use them (or use Caddy / nginx in front).

For most users this is overkill — HTTP-over-WireGuard is already E2E encrypted, the HTTPS layer is just for browser warnings.

## Multiple users on the same family Yorik

Tailscale "Users + Sharing": invite family members to your tailnet, OR create a tailnet per user and share specific machines. Free tier allows both.

Each tailnet member gets their own login to Yorik (Settings → Users → Add user). Roles control who sees what.

## The Immich mobile app needs special handling

The Immich app expects to talk to Immich on port 2283. Over Tailscale: set the server URL in the Immich app to `http://yorik-home:2283` (same as local Wi-Fi but using the Tailscale hostname). Works identically.

## Troubleshooting

- **`yorik-home` doesn't resolve**: MagicDNS not enabled, OR the device isn't on the tailnet (check Tailscale app shows "Connected").
- **Connects but Yorik shows blank**: uvicorn was started with `YORIK_BIND=127.0.0.1`, so it isn't listening on the Tailscale interface. Restart without the override (the default `0.0.0.0` is what you want for Tailscale): `bash start.sh`.
- **Phone keeps disconnecting**: Tailscale's battery-saver mode. Toggle off in the app.
- **Slow on cellular**: WireGuard is fast but the cellular last-mile latency is what it is. ~50–200ms per chat turn is normal.

## What Tailscale does NOT do for you

- **Encrypted backup**: if you want family.db backed up off-machine, that's the Yorik backup connector (Settings → Backup). Tailscale only handles connectivity.
- **Wake on LAN**: if your Yorik machine sleeps, Tailscale can't wake it. Disable sleep on the host (or use a NAS / always-on box).
- **Internet for the host**: Tailscale assumes your Yorik machine has its own internet. It's not a tunnel-out service.

## Privacy

Tailscale Inc routes coordination traffic (who-talks-to-who) but not data (which is direct-encrypted between devices). They publish reproducible builds + open-source the clients. The trust shift is from "your ISP + the cloud" to "Tailscale's coordination server" — much smaller surface, but not zero. If that's not OK, alternatives: self-hosted Headscale (drop-in replacement, fully self-hosted), or plain WireGuard (more setup, fully under your control).
