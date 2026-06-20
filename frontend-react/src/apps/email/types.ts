/** Mirrors the JSON shapes returned by backend/email_routes.py. */

export interface EmailAccount {
  id: number;
  owner_user_id: number;
  email: string;
  display_name?: string | null;
  imap_host: string;
  imap_port: number;
  imap_ssl: boolean;
  imap_username: string;
  smtp_host: string;
  smtp_port: number;
  smtp_ssl: boolean;
  smtp_starttls: boolean;
  smtp_username: string;
  enabled: boolean;
  is_default: boolean;
  last_sync_at?: string | null;
  last_error?: string | null;
  last_error_at?: string | null;
  created_at: string;
}

export interface EmailMessageRow {
  id: number;
  account_id: number;
  account_email: string;
  account_display_name?: string | null;
  message_id?: string | null;
  thread_id?: string | null;
  from_email: string;
  from_name: string;
  to_addrs: Array<{ name?: string; email: string }>;
  subject: string;
  snippet: string;
  date_received?: string | null;
  is_unread: boolean;
  is_starred: boolean;
  is_sent: boolean;
  has_attachments: boolean;
  category?: string | null;
  /** ISO datetime; set when the user snoozed the message (mig 024). */
  snoozed_until?: string | null;
  /** Rolled up from group_by_thread=true: how many messages in the
   *  thread (incl. this latest one) + whether any is unread. */
  thread_count?: number;
  thread_has_unread?: boolean;
}

export type PaperlessState =
  | null
  | "suggested"   // Tier 2 — awaiting user action
  | "auto_filed"  // Tier 1 — auto-ingested on arrival
  | "filed"       // Tier 2 confirmed — user clicked "File to Paperless"
  | "discarded"   // User declined the suggestion
  | "failed";     // Upload attempted but failed (retry available)

export interface EmailAttachment {
  id: number;
  filename?: string;
  mimetype?: string;
  size_bytes?: number;
  content_id?: string;
  is_inline: number;
  paperless_id?: number | null;
  paperless_state?: PaperlessState;
  immich_id?: string | null;
}

export interface EmailMessageDetail extends EmailMessageRow {
  body_text: string;
  body_html?: string | null;
  date_sent?: string | null;
  in_reply_to?: string | null;
  references_ids: string[];
  cc_addrs: Array<{ name?: string; email: string }>;
  attachments: EmailAttachment[];
  /** List-Unsubscribe analysis (RFC 2369 + 8058). Always present in the
   *  detail response — method='none' when the message has no header at
   *  all, so the frontend can show a disabled state or hide the button. */
  unsubscribe?: {
    method: "one_click" | "mailto" | "http" | "none";
    target: string | null;
    all_targets: string[];
  };
}

export interface EmailFolder {
  id: number;
  name: string;          // raw IMAP folder name
  display_name: string;  // prettified for sidebar
  flags: string[];       // SPECIAL-USE flags
  category: "inbox" | "sent" | "drafts" | "trash" | "spam" | "archive" | "all" | "starred" | "custom";
  total: number;
  unread: number;
}

export interface ProviderPreset {
  name: string;
  imap_host: string;
  imap_port: number;
  imap_ssl: boolean;
  imap_starttls: boolean;
  smtp_host: string;
  smtp_port: number;
  smtp_ssl: boolean;
  smtp_starttls: boolean;
  notes?: string;
  docs_url?: string;
  // True for providers that don't expose IMAP/SMTP directly and need
  // a local bridge app (Proton, sometimes Tuta). UI shows the steps
  // prominently so users don't try their web-login password.
  bridge_required?: boolean;
  bridge_steps?: string[];
}
