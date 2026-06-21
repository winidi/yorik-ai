// Mirrors backend/contacts.py's _to_contact_dict() shape. Kept narrow so
// renames in Python don't drift the frontend invisibly.

export type ContactKind = "person" | "business";
export type ContactStatus = "active" | "pending" | "spam" | "archived";

export type ChannelKind =
  | "email" | "phone" | "whatsapp" | "signal" | "telegram"
  | "sms"   | "website" | "social";

export type AddressKind = "home" | "work" | "billing" | "shipping" | "other";

export interface ContactChannel {
  id: number;
  kind: ChannelKind;
  value: string;
  label: string | null;
  verified_at: string | null;
  source: string;
  /** Upstream-provided name for this channel. WhatsApp's pushName,
   *  email's From-header name, Telegram's @username, etc. Modality-
   *  specific; used by the UI when contacts.display_name isn't a
   *  human name (still showing a JID/phone fallback). */
  display_name: string | null;
}

export interface ContactAddress {
  id: number;
  kind: AddressKind;
  line1: string | null;
  line2: string | null;
  postcode: string | null;
  city: string | null;
  region: string | null;
  country: string | null;
  label: string | null;
}

export interface Contact {
  id: number;
  display_name: string;
  /** Person identity columns (mig 045). first_name is the canonical
   *  identity field for kind='person'; last_name is optional (family
   *  members on a first-name basis, people you barely know). For
   *  kind='business', both are NULL — the business identity is in
   *  display_name + business_name + legal_name. */
  first_name?: string | null;
  last_name?: string | null;
  /** Job title / role at the linked employer ("Sachbearbeiterin", "CEO"). */
  role?: string | null;
  /** When this person works for / is reached through a business contact,
   *  points at the business's id. NULL for independent persons and for
   *  business rows themselves. */
  employer_contact_id?: number | null;
  aliases: string[];
  kind: ContactKind;
  status: ContactStatus;
  relation: string | null;
  birthday: string | null;
  language_pref: string | null;
  salutation_pref: string | null;
  legal_name: string | null;
  tax_id: string | null;
  iban: string | null;
  payment_terms_days: number | null;
  default_currency: string | null;
  notes: string | null;
  tags: string[];
  allowed_roles: string;
  created_at: string;
  updated_at: string;
  source: string;
  last_used_at: string | null;
  last_interaction_at: string | null;
  /** Manual top-of-list flag (mig 025). */
  pinned?: boolean;
  /** Per-contact opt-in for the suggestion engine (mig 121). */
  yorik_assist_enabled?: boolean;
  channels: ContactChannel[];
  addresses: ContactAddress[];
}

/** /api/contacts/{id}/timeline response. */
export interface ContactTimelineItem {
  kind: "email" | "event" | "draft";
  when: string | null;
  title: string;
  sub?: string;
  link: string;
  direction?: "incoming" | "outgoing";  // emails only
}
export interface ContactTimeline {
  contact_id: number;
  items: ContactTimelineItem[];
  total: number;
  by_kind: { email: number; event: number; draft: number };
}

export interface StatusCounts {
  active: number;
  pending: number;
  spam: number;
  archived: number;
  /** Mig 119? — number of pending rows the LLM hasn't classified yet.
   *  Drives the "Step 1: Classify · N ready" badge in the cleanup
   *  pipeline. When 0, step 1 fades to ✓ done. */
  pending_unclassified?: number;
  pending_classified?:   number;
}
