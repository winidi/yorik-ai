---
title: WhatsApp bridge — pairing + multi-device
nav_app: whatsapp
summary: Pair WhatsApp via QR scan; bridge captures chats so Yorik can read history, draft replies, auto-add contacts. Local-only, no cloud.
---

# WhatsApp bridge — pairing + multi-device

Yorik bundles a self-hosted WhatsApp bridge (built on whatsmeow). It connects to WhatsApp as a multi-device companion — exactly like WhatsApp Web does — and forwards messages to Yorik's local database.

This is opt-in. Without it, Yorik knows nothing about your WhatsApp.

## Pairing

1. Open Yorik → **WhatsApp** app.
2. Yorik shows a QR code.
3. On your phone: WhatsApp → Settings → Linked Devices → Link a Device → scan the QR.
4. Pairing confirms in ~5 seconds. The QR disappears.

## What gets captured

After pairing, Yorik's bridge receives every message the phone sees — incoming + outgoing. Messages land in `wa_messages`. Yorik can:

- Search across all your chats from the chat agent (*"was hat Anna letzten Donnerstag geschrieben?"*).
- Auto-suggest adding new contacts when someone messages you who isn't in your hub.
- Draft replies (state-based: friendly / formal / quick / warm / firm).

## What does NOT get sent out

- Yorik does NOT send messages on your behalf without explicit confirmation.
- Drafts are saved locally; you copy them into WhatsApp manually OR (v0.2 roadmap) approve them for the bridge to send.

## Unpairing

WhatsApp → Settings → Linked Devices → tap the Yorik entry → **Log out from this device**. Bridge becomes inactive. Yorik's stored history stays in the local DB until you clear it (Settings → Privacy → Clear WhatsApp history).

## Troubleshooting

- **QR doesn't scan**: brightness too low / camera at a bad angle. Try again with the screen brighter.
- **Pairs then immediately disconnects**: usually the bridge container can't reach WhatsApp's servers. Check internet access on the Yorik machine.
- **History missing**: WhatsApp's multi-device protocol only delivers messages received WHILE paired. Pre-pairing history doesn't sync (this is a WhatsApp limitation, not Yorik's).
