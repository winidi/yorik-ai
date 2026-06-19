---
name: compose_draft
description: Prepare a letter / invoice / email / offer as a draft the user can finalize in the Compose app
when_not_to_use: |
  Short chat replies, internal notes, calendar/task creation, or quick acknowledgements — those don't go through Compose. Don't use to send WhatsApp messages (→ `whatsapp_draft`) or emails (→ `email_draft`).
when_to_use: |
  Trigger: "schreibe / draft / write Brief / Mail / Rechnung / Angebot / Dokument" or any request whose output is a saveable/printable/sendable document.

  Reply ONE short sentence ("Hab den Brief vorbereitet"); the chat card carries the body — never paste the document into chat.

  ═══ POSTAL LETTER FLOW (chat-initiated) ═══

  1. `find_person(query=<name>)` first — even if the user gave you an address. 1 match → use it. Multiple → ask which. 0 matches → STOP, ask "Soll ich [name] als neuen Kontakt anlegen?", then `add_contact(display_name=<name>, kind="person" or "business")` and CONTINUE with the new contact_id. Never proceed to compose_draft without a contact — a contact-less letter is missing the recipient block by design.

  2. `compose_check_recipient(contact_id, template_id)` — returns `complete: true/false`. Never guess address completeness yourself.

  3. `compose_check_template_args(template_id, contact_id, args={...})` — extract structured slots from the user's message into `args` FIRST. Examples:
       "zum 17.08.26"           → {"beendigung_zum": "17.08.2026"}
       "Kundennummer K-1234"    → {"kunden_oder_vertragsnummer": "K-1234"}
       "weil ich umziehe"       → {"kuendigungsgrund_satz": "Anlass ist mein Umzug."}
     2-digit years map to current/next year ("26" → 2026). If complete=false the skill emits a form — WAIT for [form_submit].

  4. `compose_draft(contact_id, template_id, body="", args={...})` — pass the SAME args dict. Structured slots (dates, IDs, addresses) go in args, NEVER in body.

     WHEN to pass a real body vs body="":
       - SPECIALIZED template with structured fields (Kündigung-Vertrag, Mietminderung, etc.) → body="" is correct. The template renders proper prose from the slot values you filled.
       - GENERIC template (its `llm_hints` block, surfaced in the [template_picked] message, says so explicitly) → YOU MUST write the body. Use the user's stated intent verbatim. Empty body on a generic template renders an empty placeholder — the user always reports this as a bug.
       - When the user didn't specify amounts/dates, leave them as "[Monat]" / "[Betrag]" placeholders the user fills in the editor — better than blank.

  Step 3-ALT (address missing): try `find_recipient_address_from_documents(contact_id)` before asking the user. Candidates → ask yes/no → `add_contact_address` → continue.

  After `[form_submit from=X]` the message tells you the next step verbatim — follow it. The find_person-first flow applies to `email_draft` and `whatsapp_draft` too (no compose_check_recipient for those).

  ═══ EMAIL FLOW (chat-initiated) ═══

  When the user says "schreibe eine E-Mail / write an email" call with `kind="email"`. The template selector auto-picks the generic email template (no postal address block, no date, no formal salutation default). DO NOT pass a letter template id for an email — that renders a postal letter with all the wrong chrome.

  Email differs from postal letter in three rules — these are the ones the LLM gets wrong most often:

  1. `find_person` is OPTIONAL for email. Email only needs a name for the greeting; the recipient email address goes in the envelope (the Compose UI's "To:" field), not in the body. If `find_person` returns 0 matches, do NOT auto-call `add_contact` and do NOT silently create a phantom contact — proceed with `recipient=<name>` only, no `contact_id`. A "generische adresse" / "I don't have one yet" answer means the user wants to compose and fill in the address later — leave `recipient_address` UNSET (null), never fabricate something plausible like `max.mustermann@example.com`.

  2. The body for email is the same chrome contract as letter: greeting + middle + closing → template adds signature. NEVER write `Hallo <Name>` followed by `[Dein Name]` / `[Your Name]` at the bottom — both leak through and produce stacked salutations. Either put a greeting in `args.anrede` and a closing in `args.gruss` (recommended), or pass them in `body` ONCE and leave args empty — never both.

  3. NEVER invent a recipient email address. If the user said "generic address" / "I don't have one" / "we'll send it later", pass NO `recipient_address` and respond with one short sentence ("Hab die E-Mail vorbereitet — die Empfängeradresse trägst du im Editor ein"). Do NOT fabricate plausible-looking addresses like `<firstname>.<lastname>@beispiel.de`.

  ═══ INSIDE COMPOSE MODE ═══

  When the user message starts with `[Compose context: template=<id> | args: … | sender: … | draft_id=<N?>]`:
    - Pass `template_id` from the context. Don't substitute unless the user explicitly switches.
    - Pass `inline_form=true` — missing args render as an inline form via NeedsInputCard.
    - If `draft_id=N` is present, pass `existing_draft_id=N`. Args dict merges; send only changing keys.
    - `sender:` is authoritative — never ask for the user's own name/address/phone/signature. If `sender_address_status=missing`, say "Deine Absenderadresse fehlt im Profil" + `navigate_to(app="settings")`.
    - Don't `navigate_to(app="compose")` — they're already there.
    - For inline photos: `propose_inline_photo(query, contact_id, template_id, draft_id)`. Never paste an image URL into body. After `[photo_picked]`, call compose_draft as instructed.
    - Don't invent recipient name/address. If unknown → ask.

  ═══ VAGUE REQUEST ═══

  If the user was vague ("schreibe einen Brief"), call with `body=""`. The skill detects vagueness and emits a template picker. Don't fabricate a generic body.

inputs:
  kind:
    type: string
    required: false
    default: letter
    description: One of "letter", "invoice", "offer", "email", "memo". Drives the badge in the chat card and the default template in Compose.
  recipient:
    type: string
    required: false
    description: Recipient name ONLY ("Müller GmbH", "Dr. Lena Hoffmann", "Frau Becker"). No address — that goes in recipient_address.
  recipient_address:
    type: string
    required: false
    description: |
      Recipient's postal address as a SINGLE STRING (line-separated with \n).
      Example: "Teststraße 1\n30159 Tesstadt". Pass this when the user gave
      you an address that isn't on a contact yet — better than stuffing it
      into the body where it'll duplicate with the template's recipient
      block.

      IGNORED when `contact_id` is set — the address comes from the contact, so only pass this for one-off addresses with no contact row.
  subject:
    type: string
    required: false
    description: Subject / Betreff. For invoices, usually the work title ("Kücheneinbau"). For letters, the topic. Surfaces in the card.
  body:
    type: string
    required: true
    description: |
      The MIDDLE of the letter ONLY. Plain text, 1-3 short paragraphs. The template adds the chrome — do NOT include sender block, recipient block, date, salutation (template `anrede`), closing (template `gruss`), or signature.

      RIGHT example for "schreib Hans dass wir am Mittwoch das Auto abholen":
        "Wir möchten dir Bescheid geben, dass wir am Mittwoch das Auto in der Falkenbergerstraße abholen kommen."

      The skill defensively strips chrome but the body becomes shorter than expected when it does — send only the middle.
  template_id:
    type: string
    required: false
    description: Optional template ID hint if the user named one explicitly ("schreibe in der formellen Vorlage"). Leave blank otherwise — user can pick in Compose.
  contact_id:
    type: integer
    required: false
    description: |
      ID returned by find_person for the recipient. When set, the skill pulls
      the contact's display_name (used as `recipient`) and primary postal
      address from the identity hub and pre-fills both into the draft args
      under all common template arg keys (recipient_name, empfaenger_adresse,
      vermieter_name, locatore_indirizzo, ...). ALWAYS pass this when the
      user named a person — call find_person first.
  existing_draft_id:
    type: integer
    required: false
    description: |
      When set, UPDATE this draft in-place instead of creating a new one.
      The Compose context line carries `draft_id=N` when the user has a
      saved draft loaded in the editor — always pass that value here for
      chat-driven edits ("ändere die Anrede", "schreib das Datum auf
      31.12", "mach den Brief förmlicher"). Without this, every chat
      tweak creates a new draft row and the sidebar fills up with
      near-duplicates. The skill loads the existing draft's args first
      and merges your `args` dict on top, so you only need to send the
      fields you're CHANGING — everything else is preserved.
  args:
    type: object
    required: false
    description: |
      Generic key/value dict of template-specific args you want to set
      directly — e.g. {"beendigung_zum": "01.08.2026", "mietvertrag_vom":
      "15.06.2022", "kuendigung_zum": "31.03.2027", "betreff": "Kündigung"}.
      Use this for structured data the user gave you (dates, contract
      numbers, amounts, account numbers) so it lands in the correct
      template slot instead of being prose-padded into `body`.

      Rule of thumb: anything the user mentioned that fits a single
      named slot (a date, a number, a name) belongs in `args`. Anything
      that's free-prose middle-of-the-letter sentences belongs in `body`.

      You can read the template's expected arg keys from its
      `default_args` (visible in the compose context line) — those are
      the slots worth filling.

      TONE OVERRIDE (anrede + gruss): the bundled letter templates
      default to formal `anrede="Sehr geehrte Damen und Herren,"` +
      `gruss="Mit freundlichen Grüßen"`. When you are writing a CASUAL
      letter (user said "Freund / friend / Oma / Bruder" etc.) AND/OR
      you wrote the body in the informal du-form, OVERRIDE both in
      args so the rendered letter doesn't have a friendly body wrapped
      in formal chrome:
        args = {
          "anrede": "Hallo <First name>,"   or "Liebe <FirstName>,"   or "Lieber <FirstName>,",
          "gruss":  "Liebe Grüße"           or "Viele Grüße"          or "Herzliche Grüße",
        }
      Pick the first name from the recipient (split contact's
      display_name on whitespace, take the first token). When you
      don't know the gender, default to "Hallo <First>," — gender-
      neutral. Skip the override (keep template defaults) for formal
      letters: business, authority, Kündigung, Rechnung, anything the
      user phrased with "Sie".

outputs:
  draft_id:
    type: integer
    description: The persisted draft's id. The chat card uses this to deep-link "Bearbeiten →" to /r/compose?draft_id=N.
  recipient:
    type: string
  subject:
    type: string
  kind:
    type: string
permissions: [admin, member]
tags: [compose, letter, brief, kuendigung, rechnung, draft, email, invoice]
---

# compose_draft

Yorik's chat is for QUICK actions. Long-form documents — letters, invoices, formal email — belong in the Compose app where the user can:
- pick / switch a template
- get a proper Rechnungsnummer (GoBD-compliant audit)
- render to PDF/A-3 with embedded ZUGFeRD XML for German B2B compliance
- send via email or save to Paperless

This skill is the bridge. When the user asks for a document, you (the LLM) call this skill with the body you generated. The skill:
1. Persists a draft in `compose_drafts`
2. Emits a `compose_draft_created` UI action containing draft_id, recipient, subject, kind
3. ChatApp renders a card under your assistant message: "[ Edit → ] [ Senden ]"
4. User clicks → Compose opens with the draft loaded

Your reply text should be short — the card carries the content. One sentence acknowledging what you prepared.

Example GOOD reply: "Hab den Brief an Müller vorbereitet — schau ihn dir an."
Example BAD reply: A 200-word fully-formatted letter pasted into the chat bubble.
