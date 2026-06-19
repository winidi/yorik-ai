/** Mirrors backend/main.py /api/documents responses. */

export type DocVisibility = "private" | "business" | "shared";

export interface YorikDocument {
  id: number;
  title: string;
  mime_type?: string | null;
  bytes: number;
  tags?: string | number[] | null;
  allowed_roles: string;
  chunk_count: number;
  created_at: string;
  indexed_at?: string | null;
  // 'local' = uploaded via Yorik (lives in documents.db, previewed via
  // /api/documents/{id}/raw). 'paperless' = mirrored from Paperless
  // (preview via the /paperless/* proxy, id is negative to dodge
  // collisions — use Math.abs(id) for the proxy URL).
  source?: "local" | "paperless";
  // Paperless-only — set by the backend's tag→visibility mapping.
  visibility?: DocVisibility;
  owner?: number | null;
  via_admin_token?: boolean;
}

export interface DocumentSearchHit {
  chunk_id: number;
  doc_id: number;
  doc_title: string;
  doc_mime?: string | null;
  chunk_index: number;
  chunk_text: string;
  char_start: number;
  char_end: number;
  distance: number;
  /** Unified, source-agnostic relevance score in [0, 1]. 1.0 = top
   *  hit; the rest scale down by their RRF rank in the fused list.
   *  Drives the badge that's actually monotonically decreasing. */
  match_score?: number;
  /** "semantic" | "fts" | "hybrid" | "paperless" — which engine(s)
   *  surfaced this hit; "hybrid" means both engines ranked it. */
  match_type?: string;
}

export interface SearchLegStatus {
  /** Number of hits this engine returned (pre-fusion, pre-cap). */
  count: number;
  /** Null when the engine ran cleanly. Human-readable reason when it
   *  didn't (embedder down, no Paperless token, etc.). */
  error: string | null;
  /** Semantic-leg only: number of chunks in the vector index. 0 means
   *  the bundled embedder hasn't ingested anything yet. */
  vec_count?: number;
}

export interface SearchResponse {
  hits: DocumentSearchHit[];
  legs: {
    semantic?: SearchLegStatus;
    fts?: SearchLegStatus;
  };
  /** How many of `hits` came from Yorik's own uploads (vs. Paperless). */
  native_count?: number;
}
