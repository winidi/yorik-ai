/** Shapes of /api/writing (backend/writing/store.py). */
export interface Recipient { contact_id: number | null; name: string; address_lines: string[]; email: string }
export interface LetterContent { subject?: string; text_html?: string; add_closing?: boolean; date?: string; your_ref?: string; our_ref?: string }
export interface WrittenDocRow {
  id: number; kind: "letter" | "invoice" | "quote"; status: "draft" | "final"; title: string; recipient: Partial<Recipient>;
  number: string | null; has_pdf: boolean; paperless_doc_id: number | null; updated_at: string; finalised_at: string | null;
}
export interface WrittenDoc extends WrittenDocRow { content: LetterContent; letterhead_id: number | null }
