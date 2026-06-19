---
title: What should I do next — guided first 10 minutes
nav_app: home
summary: After the LLM is connected, the three highest-value things to do first — email accounts, bulk document import, photos. Each with a concrete starting step.
---

# What should I do next?

Yorik's chat works now. To make it actually useful you need to give it your data. Three things, in roughly this order — each takes 2–5 minutes:

1. **Connect an email account** so Yorik can summarise your inbox + draft replies.
2. **Get your documents in** — fastest if you have a folder full to import.
3. **Get your photos in** — phone autosync is the killer feature here.

You don't have to do all three. Pick the one that matters most.

---

## 1. Connect an email account (~2 min)

The setup is wizard-driven, no manual IMAP/SMTP entry needed for the common providers (Gmail, Outlook/Hotmail, GMX, iCloud, mail.de, Web.de, T-Online, Strato — all auto-detected from the address).

**Steps:**

1. Open the **Email** app (bottom-dock icon, or `/r/email`).
2. Click **"Add account"** if it's the first one (the inbox is empty otherwise).
3. Enter your email address + app password (Gmail/Outlook require an app-specific password, not your normal one — Yorik shows a link to your provider's docs on the next screen).
4. Click **Connect**. Yorik probes IMAP + SMTP, shows green checkmarks, and starts fetching messages.

The first sync grabs the last 30 days; subsequent ticks keep it live.

**What you can do immediately:**

- Ask the chat: *"gib mir eine kurze email briefing"* — Yorik summarises action items + active threads.
- Click an unread email → click **"AI drafts"** below the body → pick a tone chip → 3 reply variants appear.

See `email` for full provider-specific notes (Gmail OAuth, Apple iCloud's catch-22, etc.).

---

## 2. Get your documents in (~3 min if you have a stack)

Paperless is the storage layer. Three workflows depending on what you've got:

### A. A folder full of PDFs / scans (BULK IMPORT — recommended for first run)

This is the fastest path if you have hundreds of historical documents to import.

1. Find the consume folder: `~/yorik-ai/data/paperless/consume/`.
2. Copy or move ALL your existing PDFs / scanned images into it — there's no quantity limit. Subfolders are flattened.
3. Paperless picks them up automatically, ~one document per 5–30 seconds (depends on OCR speed). 600 documents take maybe 30–60 minutes total in the background.
4. In Yorik's **Documents** app, click the **refresh icon** in the sidebar — that triggers an immediate sync from Paperless. (Without it, Yorik's background reconciler picks them up every 6 hours.)

Watch the import progress at `/paperless/` (the Paperless web UI, available via the "Open Paperless" button on the Documents sidebar). The document count climbs in real time.

### B. One-off upload from a phone or computer

Open the **Documents** app → drag-and-drop a file (desktop) or tap the **+** button (mobile). Goes through Yorik's `/api/documents/upload` → Paperless → OCR runs in the background.

### C. Email-to-Paperless

Forward invoices etc. to a configured IMAP inbox; Paperless polls it. Set this up at Paperless web UI → Settings → IMAP. Out of scope for "first 10 minutes" — set up later when you want it.

**What you can do immediately:**

- *"finde meinen mietvertrag"* — Yorik searches semantically.
- *"wie hoch ist der Betrag in der Stromrechnung von Stadtwerke"* — Yorik reads the PDF text and answers.

See `paperless` for OCR languages, scan workflows, and tag rules.

---

## 3. Get your photos in (~5 min for autosync, instant for bulk)

Immich is the photo library. The killer feature is phone autosync — once configured, every photo you take backs up automatically.

### A. Phone autosync (recommended, set up ONCE)

1. On your phone, install the **Immich** app (Android Play Store / iOS App Store).
2. In the app: **Server URL** = `http://<your-machine-ip>:2283` (or your Tailscale hostname — see `tailscale`).
3. Log in with the Immich account Yorik created for you during setup (same email as your Yorik admin account; the password was shown once during install — check Settings → Account → Credentials).
4. App → Settings → **Background backup: ON**. Pick whether to upload over WiFi only or also on cellular.

Done. Photos taken from now on appear in Yorik within a minute.

### B. Bulk-import existing photos from a folder

If you have years of photos on disk you want imported in one shot:

1. Open the **Immich** web UI: `http://localhost:2283` (or open from Yorik's Documents app sidebar — Immich is at the dock too).
2. Top-right: **Upload** → select your photo folder (`Ctrl+A` in the file picker for everything).
3. Walk away. Immich uploads + thumbnails + runs face recognition in the background.

For >10,000 photos use the **Immich CLI** instead (faster, resumable):
```
npm install -g @immich/cli
immich upload --recursive /path/to/photos
```

**What you can do immediately:**

- *"finde fotos von Anna"* — face recognition. (You'll need to name the face once in the Immich UI first — Yorik's chat picker prompts you when needed.)
- *"foto vom letzten Wochenende"* — date-range search.
- *"finde fotos in Berlin"* — location search if your camera embeds GPS.

See `immich` for face-naming workflow, external SSD relocation, and the GPU-acceleration option.

---

## What else?

- **Tailscale** (`tailscale`) — remote access to your Yorik from phone/laptop outside the LAN. Free, takes 5 minutes.
- **WhatsApp** (`whatsapp`) — chat ingestion from your phone via the bundled bridge.
- **Voice** (`voice`) — speaker enrollment so the FAB knows who said what.
- **Backups** (`backup`) — schedule encrypted backups to an external drive. **Do this before you ingest 600 documents.**

For each of these, ask the chat: *"wie richte ich Tailscale ein?"* / *"wie funktioniert WhatsApp Bridge?"* / etc. Yorik reads from the shipped docs — answers are grounded, not guessed.
