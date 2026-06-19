/** Mirrors backend/main.py /api/compose/* responses. */

/**
 * Declarative role for a template arg. Drives every automatic behavior
 * keyed off "what is this field for" without relying on key-name regex.
 *
 * Closed enum on purpose — new roles should be added here AND wired
 * into ComposeApp/RecipientPicker/backend connectors/compose.py at the
 * same time. Templates that don't declare a role fall back to key-name
 * heuristics (works for conventionally-named fields, breaks for novel
 * ones — community contributors get a validator warning).
 *
 * See templates/SCHEMA.md for what each role unlocks.
 */
export type ArgRole =
  | "body"               // multi-line prose body
  | "subject"            // one-line subject / title
  | "greeting"           // "Sehr geehrte ..." line
  | "closing"            // "Mit freundlichen Grüßen" line
  | "recipient_name"     // who the document is to
  | "recipient_address"  // recipient postal address block
  | "recipient_email"
  | "recipient_phone"
  | "sender_name"        // who it's from
  | "sender_address"
  | "sender_email"
  | "sender_phone"
  | "sender_business"    // legal business name on letterhead
  | "date"               // ISO date or human date
  | "reference_number"   // contract / customer / invoice number
  | "currency_amount"    // monetary amount
  | "location"           // place / city / venue
  | "freeform_text"      // any other text — no special behavior, just shape
  | "freeform_value";    // any other scalar — no special behavior

export interface ComposeTemplate {
  id: string;
  name: string;
  description: string;
  version: string;
  author: string;
  vertical?: string | null;
  needs_apps: string[];
  requires_extensions: string[];
  tags: string[];
  default_args: Record<string, unknown>;
  /** Raw Jinja body template — included so the args panel can
   *  classify which args actually render in the letter (referenced via
   *  `args.<key>`) versus which are routing metadata that should drop
   *  below the hairline in the right pane. */
  body_html?: string;
  /** Editor-only notes — usage hints, legal-context disclaimers, send-via
   *  reminders. ComposeApp renders this as a card BELOW the editor.
   *  Never goes into the PDF. Light HTML (<p>, <strong>, <em>, <br>). */
  editor_notes?: string;
  /** Pre-selects SendDialog's delivery mode:
   *  - "attachment" (default) — render PDF, attach to email (formal letters)
   *  - "inline" — send body_html AS the email body (short informal mails) */
  delivery_default?: "attachment" | "inline";
  /** Field schema: which args the template author considers required +
   *  human labels / hints. Drives the readiness chip in the footer and
   *  the field labels in the Arguments panel. */
  ask_user_for_args?: Array<{
    key: string;
    label?: string;
    required?: boolean;
    pattern?: string;
    hint?: string;
    /** UI control hint — "textarea" means the arg is multi-line prose.
     *  Used by the AI write-arg button to decide which fields get a
     *  "write with AI" affordance vs which are single-line text inputs. */
    input?: "text" | "textarea";
    /** Declarative role — drives ALL automatic behaviors (AI write
     *  button, auto-subject button, contacts picker, body-no-chrome
     *  prompt rule, sender-prefill from profile). Templates without
     *  `role` fall back to regex on the key name; community-authored
     *  templates should declare role explicitly. See templates/SCHEMA.md. */
    role?: ArgRole;
  }>;
  /** Example values the editor uses on first render so the canvas
   *  shows a realistic letter shape instead of empty slots. NOT used
   *  for saved drafts. */
  preview_args?: Record<string, unknown>;
}

export interface ComposeDraftResponse {
  template: ComposeTemplate;
  html: string;
  data: Record<string, unknown>;
  args: Record<string, unknown>;
}

export interface ComposeReviseSuggestion {
  text: string;
  rationale?: string;
}

export interface ComposeReviseResponse {
  suggestions: ComposeReviseSuggestion[];
  /** Backend sets this when the local LLM is unreachable so the UI can
   *  show a real explanation instead of a generic "no suggestions" toast. */
  llm_offline?: boolean;
  error?: string;
  ok?: boolean;
}

// ── Document numbering (Lexoffice-replacement, Phase 1) ─────────────────

export interface DocumentSeries {
  id: number;
  kind: string;                 // 'rechnung' | 'angebot' | 'invoice' | ...
  name: string;                 // 'Rechnungen 2026'
  scheme: string;               // '{year}-{seq}'
  prefix: string;
  seq_padding: number;
  next_number: number;
  year_reset: boolean;
  current_year?: number | null;
  is_default: boolean;
  owner_user_id?: number | null;
  notes?: string | null;
  created_at: string;
  updated_at: string;
  preview?: SeriesPreview | null;
}

export interface SeriesPreview {
  series_id: number;
  number: number;
  year: number;
  formatted: string;
}

export interface NumberingMatch {
  series_id: number;
  kind: string;
  formatted: string;
  year: number;
  number: number;
}

export interface SeriesAllocation {
  id: number;
  series_id: number;
  number: number;
  formatted: string;
  year: number;
  document_kind?: string | null;
  consumed_by_user_id?: number | null;
  paperless_doc_id?: number | null;
  pdf_sha256?: string | null;
  title?: string | null;
  notes?: string | null;
  consumed_at: string;
}

export interface SeriesPreset {
  label: string;
  description: string;
  series: Array<{
    kind: string;
    name: string;
    scheme: string;
    prefix?: string;
    seq_padding?: number;
    year_reset?: boolean;
    is_default?: boolean;
  }>;
}
