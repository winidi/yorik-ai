---
name: find_photo
description: Search the user's Immich photo library by content (CLIP), by person, or by recency
when_to_use: |
  Reply ONE short sentence; never list filenames or describe photos in prose — the photos_found cards are the answer.

  ═══ TWO HARD RULES FOR `query` (CLIP content search) ═══
  1. **Translate to ENGLISH before calling.** Immich's CLIP model (ViT-B-32 OpenAI) was trained on English captions only. German / French / Spanish queries return semantically-random results because the model never saw those tokens. The user types "in einem Anzug" → you call with query='suit'. "am Strand" → query='beach'. "im Schnee" → query='snow'. ALWAYS English, even when the user's prompt is in another language.
  2. **Never include person names.** CLIP cannot match faces. A name in `query` either matches nothing or accidentally matches based on filename text. Names ALWAYS go in `person` or `people` — those use Immich's face-recognition index. "Dirk in einem Anzug" → query='suit', person='Dirk' (NOT query='Dirk Anzug', NOT query='Dirk suit').

  - User asks for a photo of a specific person ("show me a picture of Sara", "zeig mir Fotos von Oma", "find pictures of Anna from last summer") → call with op='of_person', person='<the name>'. Immich's face recognition handles the matching. DON'T put the name in `query` and use op='search' — that's CLIP text search which won't filter by face.
  - Generic content search ("photos from the beach", "Bilder vom Sonnenuntergang") → op='search' with query=<content>.
  - **A specific date** ("photos from 23. März 2007", "Bilder vom Sonntag", "Fotos vom 12. Juni") → pass `date='YYYY-MM-DD'`. The skill expands to a 00:00–23:59 window and auto-routes to op='taken_on' which uses Immich's EXIF taken-date filter. NEVER use op='search' with a date in `query` — that's text content search and returns random matches.
  - **A date range** ("photos from last March", "Fotos aus dem Sommer 2020") → pass `start_iso='YYYY-MM-DDT00:00:00'` and `end_iso='YYYY-MM-DDT23:59:59'`. Auto-routes to op='taken_on'.
  - "Any photos from this week?" / "recent photos" → op='recent'.
  - **"How many photos do I have"** / "wie viele Fotos habe ich" — call with op='stats' (or just op='recent', which also returns counts). The result includes total_photos, total_videos, total_assets library-wide. NEVER answer "I don't have access to the count" — this skill provides it.
  - Another skill (whatsapp_send_with_photo) needs to attach a relevant image

  When results.length >= 1 the skill emits `photos_found` automatically — do
  NOT enumerate filenames or describe the photos in text. Reply ONE short
  sentence ("Drei Fotos aus Italien, siehe Karten unten"). When results.length
  == 0, the skill stays silent; say so briefly in text ("Keine Fotos gefunden").

  ═══ METADATA FILTERS (combine freely) ═══

  Any combination of these auto-routes to op='filter' and runs as ONE Immich query — no need to call the skill multiple times or pick the "right" op. Combine with `person`, `start_iso`/`end_iso`/`date` freely:

  - **Location** ("aus Berlin", "vom Urlaub in Italien", "Fotos aus München") → `location='Berlin'`. Tries Immich's city → state → country fields in order. Place names only — for descriptive places like "am Strand" use op='search' with CLIP instead.
  - **Favorites** ("meine Lieblingsfotos", "die mit Herz") → `favorites_only=true`.
  - **Album** ("aus dem Album Hochzeit", "vom Roadtrip-Album") → `album='Hochzeit'`. Case-insensitive substring match.
  - **Photos vs videos** ("nur Fotos", "nur Videos") → `media_type='image'` | `'video'`. Default returns both.
  - **Camera** ("vom iPhone", "von der Canon") → `camera_make='Apple'` or `camera_model='iPhone 14 Pro'`.

  Examples (combine any subset):
    "Fotos von Dirk in einem Anzug" / "photos of Dirk in a suit"
        → op='search', person='Dirk', query='suit'
          (CLIP content + face filter AND'd in one Immich call. Note
          query='suit' — ENGLISH — even though the user asked in German.
          Do NOT collapse into query='Dirk Anzug' or query='Dirk suit'
          — CLIP doesn't know who Dirk is, and the German word returns
          semantically-random results.)
    "Bilder vom Sonnenuntergang am Strand" → op='search', query='sunset at the beach'
          (German prompt → English query. Always.)
    "Foto im Anzug vor dem Spiegel" → op='search', query='suit in front of a mirror'
    "Lieblingsfoto von Sara aus Italien"
        → favorites_only=true, person='Sara', location='Italien'
    "Fotos vom Roadtrip 2023 in Spanien"
        → start_iso='2023-01-01T00:00:00', end_iso='2023-12-31T23:59:59', location='Spanien'
    "Videos vom Geburtstag aus dem Album 2024"
        → media_type='video', album='Geburtstag 2024'
    "drei iPhone-Fotos von Sara aus Hannover"
        → person='Sara', location='Hannover', camera_make='Apple', take_count=3
    "Bilder von mir und Sara" (user is Tom)
        → people='Tom, Sara'   (Immich AND's them — BOTH faces required)
    "Foto von uns dreien letzten Sommer in Italien" (user is Tom; +Sara, Anna)
        → people='Tom, Sara, Anna', location='Italien',
          start_iso='2024-06-01...', end_iso='2024-08-31...'
inputs:
  query:
    type: string
    required: false
    description: |
      Free-text content query (Immich CLIP semantic search).
      MUST be in ENGLISH — CLIP was trained on English captions only;
      German / French / Spanish queries return semantically-random
      results. Translate before calling: "Anzug" → "suit", "am Strand"
      → "at the beach", "im Schnee" → "in the snow".
      ONLY the content the user is looking for — never names, never
      dates. Names go in `person` / `people` (face-recognition index),
      dates in `date` / `start_iso`+`end_iso` (EXIF index). Examples:
      query='suit', query='sunset at the beach', query='red car',
      query='cake'. NOT query='Dirk suit', NOT query='Anzug', NOT
      query='2007'.
  op:
    type: string
    required: false
    default: search
    description: |
      One of 'search' (content CLIP) | 'recent' (newest first) |
      'of_person' (face-filtered) | 'taken_on' (EXIF date filter — needs date or start_iso+end_iso) |
      'filter' (combined metadata — location / favorite / album / camera / media_type; auto-routed when any of those are set) |
      'stats' (counts only). Auto-routing — any metadata filter wins; pure date alone falls through to 'taken_on'.
  person:
    type: string
    required: false
    description: Named person for op='of_person'. Must match a face cluster you've labelled in Immich. For multiple people, use `people` instead.
  people:
    type: string
    required: false
    description: |
      Multiple people the photo must contain — pass either a list ["Sara", "Tom"]
      or a comma-separated string ("Sara, Tom"). Auto-routes to op='filter'.
      Immich AND's them — "Sara, Tom" returns photos with BOTH faces. Use this
      for "Fotos von mir und Sara" / "photos of me and X". Substitute "me / ich"
      with the logged-in user's first name (see WHO 'ME' IS in the system prompt).
  date:
    type: string
    required: false
    description: A single calendar day, format YYYY-MM-DD. The skill expands to a 00:00–23:59 window and auto-routes to op='taken_on'. Easiest way to filter by date — "photos from 23. März 2007" → date='2007-03-23'.
  start_iso:
    type: string
    required: false
    description: Range start as ISO datetime YYYY-MM-DDTHH:MM:SS. Pair with end_iso. For a single day, use `date` instead.
  end_iso:
    type: string
    required: false
    description: Range end as ISO datetime YYYY-MM-DDTHH:MM:SS. Pair with start_iso.
  days:
    type: integer
    required: false
    default: 7
    description: Look-back window for op='recent'
  take_count:
    type: integer
    required: false
    default: 12
    description: Max results (1-30)
  location:
    type: string
    required: false
    description: Place name — city / state / country (e.g. "Berlin", "Italy", "Bayern"). Auto-routes to op='filter'. Combine with date/person/etc. For descriptive places ("am Strand") use op='search' with CLIP instead.
  favorites_only:
    type: boolean
    required: false
    default: false
    description: Only return photos the user has marked as Favorite in Immich. Auto-routes to op='filter'.
  album:
    type: string
    required: false
    description: Restrict to a single album by name (case-insensitive substring match). Auto-routes to op='filter'.
  media_type:
    type: string
    required: false
    description: One of 'image' | 'video'. Default returns both. Auto-routes to op='filter'.
  camera_make:
    type: string
    required: false
    description: Camera manufacturer ("Apple", "Canon", "Sony"). Auto-routes to op='filter'.
  camera_model:
    type: string
    required: false
    description: Specific camera model ("iPhone 14 Pro"). Auto-routes to op='filter'.
outputs:
  photos:
    type: array
    description: |
      One element per match, each with:
        - id (str) — Immich asset id (use as `immich_asset_id` for downstream send)
        - original_name (str)
        - taken_at (str) — ISO date
        - type (str) — 'IMAGE' or 'VIDEO'
        - thumbnail_url (str) — pre-authenticated, browser-ready
        - view_url (str) — Immich web UI link
cost: 1 Immich API call (no LLM)
permissions: [admin, member, restricted]
side_effects: none — read-only
tags: [immich, photos, search, clip]
---

# find_photo

Thin wrapper around the existing Immich connector. CLIP-based content
search means a query like "beach sunset" finds matching photos even
without tags or captions. `of_person` requires you to have named your
face clusters in Immich's People page first.

Pairs with the future `whatsapp_send_with_photo` composite skill:
the `id` field this skill returns is the `immich_asset_id` that skill
consumes — no JID translation, no separate auth, native composition.
