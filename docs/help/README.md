# Yorik help docs

User-facing setup + usage guides, read by the `yorik_help` skill so the chat agent can guide new users without hallucinating.

One short markdown file per topic. Each starts with YAML frontmatter declaring:
- `title` — human label
- `nav_app` (optional) — Yorik app to suggest jumping to (`settings`, `calendar`, `compose`, etc.)
- `nav_query` (optional) — query params for the deep link
- `summary` — one-sentence what's in here, shown to the LLM in the skill catalog

The skill loads all files at boot, keys them by filename (`01-first-run.md` → topic `first-run`), and returns the body verbatim plus the navigation hint when invoked.

## Topics shipping with this release

| Topic | What it covers | Status |
|---|---|---|
| `next-steps` | The three highest-value tasks right after the LLM is connected: email, documents, photos | Full |
| `first-run` | What the user should do after first launch | Full |
| `llm-setup` | Connecting / changing the LLM endpoint | Full |
| `paperless` | Adding documents, scan workflow, search | Full |
| `immich` | Mobile app pairing, photo upload, library | Full |
| `tailscale` | Remote access from phone / other devices | Full |
| `voice` | Voice profiles, calibration, dictation | Stub |
| `whatsapp` | Bridge pairing, multi-device | Stub |
| `email` | IMAP/SMTP setup, sending drafts | Stub |
| `compose` | Writing letters, invoices, templates | Stub |
| `contacts` | Importing, editing, sharing | Stub |
| `calendar` | Events, sharing, attendees | Stub |
| `tasks` | Recurring tasks, briefing integration | Stub |
| `briefing` | Morning routine, what's included | Full |
| `themes` | Visual customisation per app | Full |
| `extensions` | ZUGFeRD, regional add-ons | Full |
| `troubleshooting` | Common errors + fixes | Full |

## Authoring style

- Address the user directly ("you"). Plain language.
- Step-by-step instructions where applicable.
- Reference the actual UI labels (German where the app uses German).
- Be honest about what's bundled vs BYO vs roadmap.
- ≤200 lines per file; if a topic needs more, split it.
- No emojis. No real names in examples — use placeholders.
