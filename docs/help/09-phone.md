---
title: Yorik on a phone
nav_app: settings
nav_query:
  tab: start
summary: How a family member gets Yorik on their phone — invite by QR code, sign in with a PIN; outside the house through Tailscale.
---

# Yorik on a phone

## For a family member (the usual way)

1. An admin opens Settings → **Start here** → **Invite** (or Settings → Household → People → Invite).
2. A QR code appears. The family member scans it with the phone's camera while on the home Wi-Fi.
3. The phone opens Yorik; they pick their name, a colour and a four-digit PIN. Done — Yorik now opens on their phone like an app (Home → Chat, Calendar, Tasks, More).

Nothing to install, no account to create. Children get a shorter Yorik with their own board, tasks, calendar and photos.

## From outside the house

At home the phone reaches Yorik over the Wi-Fi. For the train, work or holidays, the admin sets up Tailscale once: Settings → Maintenance → **Health & updates** → **Phones** shows the steps and checks them. The family members then install the Tailscale app on their phones and sign in with the family's Tailscale account; Yorik keeps working with the same address. Details: `tailscale`.

## If it does not open

- The phone must be on the same Wi-Fi for the QR invite.
- A PIN is four digits; forgotten → the admin resets it under People.
- See `troubleshooting` for the rest.
