---
name: propose_inline_photo
description: "Find candidate photos and show a visual picker so the user can include one inline in a letter."
when_to_use: |
  When the user wants to include a photo INSIDE a letter (not as a
  separate attachment to an email — that's just file-upload UX).
  Typical phrasings: "schreib einen Brief an Hans mit dem Foto vom
  Urlaub", "hänge ein Bild vom letzten Wochenende an", "send Anna a
  letter and include a vacation photo".

  Flow:
    user: "schreib Hans einen Brief mit einem Foto vom Italien-Urlaub"
    you:  find_person("Hans") → resolved
    you:  propose_inline_photo(query="Italien Urlaub", contact_id=42,
                                template_id="generic-letter")
          → emits photo_picker ui_action with thumbnails
    user: clicks the photo they want
    you:  compose_draft(contact_id=42, template_id="generic-letter",
                        args={inline_image_url: "<picked url>",
                              inline_image_caption: "Urlaub Sizilien 2024"})

  The skill auto-derives a sensible query if you don't pass one (e.g.
  the contact's name, recent photos). The picker resumes the playbook
  by giving you the picked photo's URL in a [photo_picked] message.

  Important: pass `template_id` so the picker knows which template's
  image slot to fill. Pass `draft_id` if a draft is loaded so the
  resume goes back to compose_draft with existing_draft_id.
inputs:
  query:
    type: string
    required: false
    description: |
      CLIP search query — what the photo should be of. "Italien
      Urlaub", "vacation Sicily", "Anna's birthday". Omit to get
      recent photos.
  contact_id:
    type: integer
    required: false
    description: Recipient contact id (so the picker's resume routes back to compose_draft with the right recipient).
  template_id:
    type: string
    required: false
    description: Active template id (so the resume keeps the same template).
  draft_id:
    type: integer
    required: false
    description: Active draft id (so the resume UPDATES that draft instead of creating a new one).
  limit:
    type: integer
    required: false
    default: 6
    description: How many candidate photos to show (3-12 sensible). The picker grid is 3 wide.
outputs:
  candidates:
    type: array
    description: List of candidate photos with id + thumbnail_url + taken_at + caption-suggestion. Empty when no photos matched.
permissions: [admin, member, restricted]
side_effects: emits photo_picker ui_action; no DB writes.
tags: [compose, photos, magic]
---

# propose_inline_photo

The bridge between Immich and Compose. The user describes the photo
they want and gets a 1-click visual picker — no copy-pasting URLs, no
hunting through the Photos app. After they pick, the playbook resumes
straight back to compose_draft with the URL set as
`args.inline_image_url`.

## How the picked photo lands in the PDF

The picked photo's URL is a Yorik proxy URL (`/api/photos/{id}/raw`).
When `compose_draft` sees an inline_image_url that isn't already a
data URL, it fetches the bytes via the proxy server-side and embeds
them as a base64 `data:` URL in the rendered HTML — so the PDF is
self-contained, Gotenberg doesn't need to fetch anything externally,
and the draft is portable.
