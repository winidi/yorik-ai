---
name: yorik_help
description: "Return Yorik's setup/how-to docs for a topic (compose, immich, paperless, voice, tailscale, etc.)."
when_to_use: |
  Call this whenever the user asks how Yorik works, how to set something up, or what a feature does — instead of guessing or describing what you'd build. The corpus is curated, current, and honest about what's bundled vs BYO vs roadmap. Trigger phrases (DE + EN):
  - "wie richte ich X ein", "wie funktioniert X", "wie kann ich X", "wo finde ich"
  - "how do I set up X", "how does X work", "where do I", "help with X"
  - "what should I do next", "where do I start", "what now", "getting started"
  - "wie geht es weiter", "was als nächstes", "wo fange ich an"

  Map intent to topic:
  - "What should I do next" / "where do I start" / "I just connected the LLM, now what" → topic="next-steps" (this is the post-LLM-connect onboarding walk-through; covers email, documents, photos)
  - First steps after install, install banner walk-through → topic="first-run"
  - LLM endpoint, model, API key, Ollama, llama-swap → topic="llm-setup"
  - Documents, scans, PDFs, OCR, Paperless → topic="paperless"
  - Photos, camera roll, face recognition, Immich → topic="immich"
  - Remote access from phone, VPN, accessing from outside → topic="tailscale"
  - Voice button, dictation, Whisper, voice profile → topic="voice"
  - WhatsApp, messages, pairing phone → topic="whatsapp"
  - IMAP, SMTP, email sending, mail account → topic="email"
  - Letter, invoice, template, Brief, Rechnung → topic="compose"
  - Address book, vCard import, contact fields → topic="contacts"
  - Events, appointments, sharing calendars → topic="calendar"
  - Todo, recurring tasks, task list → topic="tasks"
  - Morning summary, daily overview, Tagesplan → topic="briefing"
  - Dark mode, colors, look and feel → topic="themes"
  - ZUGFeRD, regional add-ons, optional modules → topic="extensions"
  - Errors, "not working", debug, logs → topic="troubleshooting"

  If you don't know which topic, pass `query` with the user's words — the skill ranks topics by keyword match.

  The skill returns the doc body verbatim AND emits an "open app" button card. Quote the relevant 2-5 sentences in your reply (translate to the user's language if needed). DO NOT also call `navigate_to` — the user clicks the button when they're ready; auto-navigating yanks them off the chat screen before they finish reading.
when_not_to_use: |
  - The user asks about THEIR data ("wie viele Rechnungen habe ich") — that's `check_bills` / `find_*` / etc.
  - The user wants to DO something now ("schreib mir einen Brief") — call the doing skill directly.
  - General world-knowledge questions unrelated to Yorik — answer from your own knowledge.
  - Asking for source code or implementation details — that's not in this corpus; defer to the repo.
inputs:
  topic:
    type: string
    required: false
    description: |
      One of the topic ids: first-run | llm-setup | paperless | immich
      | tailscale | voice | whatsapp | email | compose | contacts
      | calendar | tasks | briefing | themes | extensions | troubleshooting.
      Pick the closest one to the user's question.
  query:
    type: string
    required: false
    description: |
      Free-text user intent words, used to rank topics when you're not
      sure which to pick. Used only when `topic` is empty or unknown.
outputs:
  topic:
    type: string
    description: The resolved topic id.
  title:
    type: string
    description: Human title of the doc.
  body:
    type: string
    description: The markdown body of the doc (after frontmatter).
  nav_app:
    type: string
    description: Optional Yorik app to suggest navigating to next.
  nav_query:
    type: object
    description: Optional URL params for the deep link.
  available_topics:
    type: array
    description: Full list of topic ids — useful when no topic matched.
permissions: [admin, member, restricted]
side_effects: none — read-only filesystem scan of docs/help/*.md (cached)
cost: One in-memory dict lookup; corpus is loaded once at skill init.
tags: [help, onboarding, docs, support]
---

# yorik_help

The "how do I use Yorik" answering primitive. Yorik ships a hand-written help corpus at `docs/help/` covering setup, every bundled service, and common troubleshooting. This skill is the agent's read-only window into that corpus — so chat / voice can guide a new user through onboarding without making things up.

After this skill returns:
1. Quote the relevant sentences in your reply (the user's language).
2. Do NOT dump the full body verbatim — extract what's needed for the user's question.
3. Do NOT call `navigate_to` — the skill emits a click-through button card so the user opens the app when they're ready.

If the requested topic isn't recognised, the skill returns `available_topics`. Suggest the closest matches to the user.
