---
title: Photos — Immich setup + mobile app pairing
nav_app: photos
summary: Pair your phone's camera roll to Yorik via the Immich mobile app. Face naming, search by people/places/dates, inline letter photos.
---

# Photos — Immich setup + mobile app pairing

Yorik bundles Immich as the photo store. Phone uploads, face recognition, location, CLIP semantic search — all of it works offline against your local Immich.

## Where Immich runs

Bundled by default. `bash start.sh` brings up the Immich containers (immich-server, immich-machine-learning, immich-redis, immich-postgres). Web UI at `http://localhost:2283` for direct access.

If Immich was already running when you ran `start.sh`, Yorik talks to your existing instance.

## Upload photos from your phone (most important step)

This is what most users actually want — phone camera roll syncing to Yorik automatically. Works on Android and iOS.

1. **Install the Immich app** from the Play Store (Android) or App Store (iOS).
2. **Enter the server URL** `http://<your-yorik-machine-ip>:2283`.
   - On the same Wi-Fi as the Yorik machine, this just works.
   - Find your Yorik machine's IP with `ip addr` on Linux or `ifconfig` on macOS.
   - From outside the house (mobile data, holiday, friend's Wi-Fi): install Tailscale on both the Yorik machine and the phone (see the `tailscale` doc), then enter `http://<your-tailscale-hostname>:2283` — for example `http://yorik-home:2283` with MagicDNS enabled, or the raw `100.x.y.z` tailnet IP otherwise. Immich's port (`2283`) is reachable over the tailnet automatically; no extra Yorik config needed.
3. **Sign in** with the Immich admin credentials. First-launch credentials live in `data/.immich-admin-pw` on the Yorik machine — change the password on first login.
4. **Turn on Auto backup** in the Backup tab. Pick which albums to sync (usually just **Camera**). Wi-Fi-only is the default; switch to "use mobile data" only if you want to upload over cellular.

That's it. Photos uploaded from your phone appear in Yorik's Photos app and are searchable from chat within ~30 seconds.

### What gets uploaded

By default: the entire camera roll, in original quality. Albums you toggle off don't upload. Deleted on phone → Immich keeps the copy.

### Multiple family members

Each person gets their own Immich user account (Immich UI → Account → Add user). Each phone signs in with its own user. Each user sees only their own library — except shared albums which work across users.

## Face recognition

Immich runs face recognition locally (no cloud). After ~50–100 photos uploaded, the **People** tab in Immich starts grouping faces. You name them:

1. Immich web UI → **People** → click an unnamed face cluster
2. Type the person's name
3. From now on Yorik's chat can search by that name: *"zeig mir Fotos von Anna aus dem Urlaub"*

If Yorik can't find a name in Immich, it surfaces a face-picker card asking *"is this Anna?"* — pick the right face, Yorik labels it for next time.

## Searching in chat

Ask Yorik:

- *"Zeig mir Fotos vom letzten Urlaub"* — combines location + date + recency.
- *"Fotos von mir und Anna in Italien"* — face AND face + location.
- *"Drei iPhone-Fotos vom Geburtstag"* — camera type + event + count cap.
- *"Letztes Foto von der Wohnung"* — single most-recent match.

Yorik's `find_photo` skill calls Immich's CLIP-based search + face matching. Returns photo cards inline in chat. Click a card → preview opens.

## Adding a photo to a letter / invoice (Compose)

In Compose:

1. Click the **photo** button in the toolbar (small image icon).
2. Type a search term (*"Klimaanlage Wartung 2026"*).
3. Pick from the 3-up thumbnail grid.
4. Photo lands at cursor position, embedded as a data: URL — survives the PDF render.

Use case: invoice with proof-of-work photos, letter with a scan of damaged goods, etc.

## Privacy

Photos never leave the Yorik machine unless YOU configure something. Immich's "external library" feature would let you mount a network share; bundling stays local. No cloud sync, no telemetry.

## Troubleshooting

- **Mobile app can't reach server**: check the IP. On a router that uses mDNS, `yorik.local` may work as a hostname. Outside the local network → set up Tailscale (see `tailscale`).
- **CLIP search returns no results for an obvious query**: the machine-learning container can take 60–90 seconds to load the model on first run. Wait, retry.
- **Faces never get clustered**: the machine-learning container needs to be running. Check `docker ps | grep immich-machine-learning`.
- **Yorik says "couldn't find Anna"**: name the face in Immich first (see Face recognition above).

## GPU acceleration (optional)

Immich's CLIP + face recognition runs on CPU by default. If you have an Nvidia GPU + the host has the CUDA runtime, set `YORIK_IMMICH_GPU=nvidia` in `config.env` and restart. Search becomes ~10× faster.

## Direct Immich access

`http://localhost:2283` for the full Immich web UI — bulk-tag, manual face assignments, shared albums, etc. Yorik uses Immich's REST API; never delete a photo via Yorik (the API is wired read-only). For deletion, use Immich directly.
