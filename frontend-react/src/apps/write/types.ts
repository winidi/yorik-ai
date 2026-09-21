/** Shapes of /api/writing (backend/writing/store.py). */
export interface Recipient { contact_id: number | null; name: string; address_lines: string[]; email: string }
export interface InvoiceLine { text: string; qty: string; unit: string; unit_price: string; vat_percent?: string }
export interface InvoiceContent {
  subject?: string; lines?: InvoiceLine[]; intro_html?: string; closing_html?: string; date?: string; service_from?: string; service_to?: string;
  due_date?: string; valid_until?: string; customer_no?: string; number?: string; e_invoice?: { format: string | null; checked: boolean } | null;
}
export interface DocState {
  missing: string[]; next_number: string | null;
  totals?: { net: string; gross: string; vat_rows: Array<{ rate: string; vat: string }>; lines: string[]; small_business: boolean };
  e_invoice: { wanted: boolean; available: boolean; result: { format: string | null; checked: boolean } | null } | null;
}
export interface LetterContent { subject?: string; text_html?: string; add_closing?: boolean; date?: string; your_ref?: string; our_ref?: string }
export interface WrittenDocRow {
  id: number; kind: "letter" | "invoice" | "quote"; status: "draft" | "final"; title: string; recipient: Partial<Recipient>;
  number: string | null; has_pdf: boolean; paperless_doc_id: number | null; updated_at: string; finalised_at: string | null;
}
export interface WrittenDoc extends WrittenDocRow { content: LetterContent & InvoiceContent; letterhead_id: number | null; source_document_id?: number | null }
