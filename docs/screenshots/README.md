# Screenshots

Captured screenshots / GIFs / short videos referenced from the README
and other docs. Kept in-repo so they survive a fork without external
hosting.

## Shipped (referenced from README's "What it looks like" section)

| File | Surface | What's captured |
|---|---|---|
| `home.png` | Home (hero) | "Good morning, Dirk." Apps grid, system status, background workers, quick actions. |
| `chat-documents.png` | Chat | "Find me the last invoice" → top 3 document hits as inline cards with citations to Paperless. |
| `compose.png` | Compose | Template picker sidebar, DIN 5008 letter preview, args panel (recipient address, betreff, signature) on the right. |
| `calendar.png` | Calendar (week view) | Week view with event chips, conflict-aware travel-time blocks, side panel showing the day's agenda. |
| `chat-photos.png` | Chat | "What's my last picture taken?" → actual photo rendered inline from Immich via Yorik's proxy. |

## Wishlist (still to capture)

Demo material we'd like but haven't shipped yet — PRs welcome.

| File | Used by | What to capture |
|---|---|---|
| `compose-photo-flow.gif` or `.mp4` | README — photo picker bullet | The full propose_inline_photo → 3-up thumbnail picker → photo lands in the letter, in under 20 seconds. The single most magical demo. |
| `calendar-travel-time.png` | README — travel times bullet | A calendar event card with the amber 🚗 badge ("↗ 23 min · leave 13:35") visible + the location chip below. |
| `web-result-card.png` | README — web search bullet | A web_search result rendered as the inline card in chat with title + URL + snippet + "Details holen" button. |
| `price-summary-receipt.png` | README — magic moment | The compute_group_price receipt card with line items + total + source URL footer. |
| `drafts-tab.png` | README — drafts persist bullet | The Compose sidebar with the Drafts tab active showing 3-4 cards with recipient + subject + relative time. |

## How to capture

Tools:
- **Static screenshots**: any OS screenshot tool. Crop tight. 2× DPI is fine — the README scales them.
- **GIFs / short videos**: [Peek](https://github.com/phw/peek) on Linux,
  Cleanshot X on macOS, or just QuickTime → ffmpeg conversion. Keep under
  4 MB; the README is mostly read on phones now.

Demo data: run `POST /api/demo/seed` (or hit the "Seed demo data" toggle on
the Home app) first so the screenshots have realistic-looking content
without anyone's actual data leaking in. The demo seed includes:
fictional contacts (Hans Becker, Lena Hoffmann), example events with
travel-time-computable locations (Berlin → Hannover etc.), and a couple of
templated drafts.

Naming convention:
- Use kebab-case filenames matching the table above
- PNG for static, GIF for under-30-second loops, MP4 for longer
- Width: ~1600px or less so they don't blow up the page on retina

When you add or update one, update the table above + the README
references in the same PR.
