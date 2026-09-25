---
title: Finance — bank accounts, read-only
nav_app: finance
summary: Connect a German bank account (FinTS, read-only) and see balances, transactions and spending by category. Yorik never moves money — that always stays in your bank's own app.
---

# Finance — bank accounts, read-only

Yorik can connect to a German bank account via FinTS (the standard German banks use for third-party software) and pull in your recent transactions so you can ask "what did I spend on groceries this month" in chat, or just look at the list. It cannot initiate a transfer — moving money always stays in your bank's own app or website.

## Switching it on

Finance is an optional app. An admin switches it on under **Settings → Apps**; until then there is no Finance tile in the dock and Yorik does not offer to look up transactions in chat.

## Connecting an account

Open the **Finance** app and tap **Konto verbinden**. You will need:

- Your bank's FinTS server URL (look it up by bank name at [hbci-zka.de](https://www.hbci-zka.de/))
- Your 8-digit Bankleitzahl (BLZ)
- Your online-banking username/Zugangsnummer
- Your online-banking PIN

**The PIN is typed directly into this form and goes straight to Yorik's server — it is never sent through chat, never seen by an assistant, never stored anywhere else.** It is encrypted at rest the same way your email passwords are.

If you have not received your own FinTS Produkt-ID from the Deutsche Kreditwirtschaft yet (registration takes 10–15 business days), leave the Produkt-ID field empty — Yorik falls back to a commonly-used placeholder. This is not an official value and isn't guaranteed to work with every bank; swap it out for your real one the moment it arrives.

## Private or shared

Tick **Gemeinsames Konto** when adding an account that belongs to more than one person in the household (a joint account). It becomes visible to everyone with access to the household's Finance space. Leave it unticked for a personal account — only you ever see it, the same rule as everywhere else in Yorik: nobody, not even an admin, gets a back door into another person's private account.

## What's synced

Yorik pulls transactions in the background every few hours, so a chat question never has to wait on your bank or trigger a TAN prompt mid-conversation. The first sync after connecting an account looks back 6 months; later syncs top up the last two weeks (overlapping bookings are recognised and not duplicated).

Each transaction gets a rough category (Lebensmittel, Wohnen, Abos, …) from a small built-in keyword list. Anything that doesn't match stays "unkategorisiert" — this is expected for a first version and will show up honestly rather than being guessed at.

## Asking in chat

Once an account is connected: "zeig mir meine Kontoumsätze", "was hab ich für Lebensmittel ausgegeben diesen Monat", "wie ist mein Kontostand". Yorik answers from the last sync, not a live bank query.

## Good to know

- Read-only, by design, everywhere — there is no skill, button or API route in Yorik that can move money.
- A wrong PIN or an unreachable bank URL is rejected immediately when you try to connect; nothing is saved until the connection actually works.
- Removing an account deletes its stored credential and its transaction history.
