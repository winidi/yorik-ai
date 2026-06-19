/** Mirrors backend/main.py /api/conversations + /api/ask responses. */

export interface ConversationSummary {
  id: string;
  /** LLM-generated title; null until the loop's auto-title step runs
   *  (after the second assistant reply). Sidebar/header prefer it over
   *  the raw preview when present. */
  title?: string | null;
  preview: string;
  message_count: number;
  /** Sticks the thread above the date groupings in the sidebar. */
  pinned?: boolean;
  created_at: string;
  updated_at: string;
}

/** /api/chat/mentions response — per-type matches for the @-mention
 *  popover in the composer. */
export interface MentionResults {
  contact: MentionItem[];
  event:   MentionItem[];
  doc:     MentionItem[];
}
export interface MentionItem {
  id: number;
  label: string;
  sub?: string;
}

/** Thin tool trace attached to every assistant turn (always on; the
 *  fat `agent_trace` is still dev-mode-gated). Drives the one-line
 *  "📞 find_contact · 🗓 add_event" hint under each assistant bubble. */
export interface ToolTraceEntry {
  name: string;
  args: Record<string, unknown>;
  result?: string | null;
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  /** Document hits surfaced by `search_documents` for this assistant turn. */
  documents?: DocumentHit[];
  /** Photo hits surfaced by `find_photo` for this assistant turn. */
  photos?: PhotoHit[];
  /** Non-document UI actions returned alongside this turn (refresh, navigate). */
  ui_actions?: UiAction[];
  /** Thin trace of tools called for this turn (always on). */
  tool_trace?: ToolTraceEntry[];
  /** Optional SQL the agent ran — useful to surface in a debug toggle. */
  sql_used?: string | null;
  /** Per-iteration agent trace — only present when the user has dev mode ON.
   *  Rendered as a collapsible '▼ Debug' pane below the assistant message. */
  agent_trace?: AgentTrace;
}

export interface AgentTrace {
  total_iterations: number;
  total_tool_calls: number;
  total_duration_s: number;
  from_cache: boolean;
  iterations: AgentTraceIteration[];
  halted?: boolean;
  note?: string;
}

export interface AgentTraceIteration {
  n: number;
  llm_s: number;
  duration_s: number;
  final?: boolean;
  content_len?: number;
  tool_calls: AgentTraceToolCall[];
  usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number } | null;
}

export interface AgentTraceToolCall {
  name: string;
  args: Record<string, unknown>;
  result?: string;
  ui_actions?: string[];
  duration_s: number;
  blocked?: boolean;
}

export interface PhotoHit {
  id: string;
  thumbnail_url: string;
  original_name?: string;
  taken_at?: string;
  type?: string;
}

export interface Conversation {
  id: string;
  /** LLM-generated title (null until auto-titled). */
  title?: string | null;
  user_role: string;
  messages: ChatMessage[];
  created_at: string;
  updated_at: string;
}

export interface DocumentHit {
  doc_id: number;
  title: string;
  mime_type?: string | null;
  snippet?: string;
  distance?: number;
  // "paperless" hits preview through /paperless/api/documents/{id}/preview/;
  // anything else (local uploads) through /api/documents/{id}/raw.
  source?: "local" | "paperless";
}

export interface UiAction {
  type: string;
  [k: string]: unknown;
}

/** Raw response shape from POST /api/ask. */
export interface AskResponse {
  response: string;
  sql_used?: string | null;
  rows_preview?: Record<string, unknown>[] | null;
  ui_actions?: UiAction[];
  conversation_id: string;
  from_cache?: boolean;
  /** Thin tool trace (always on; mirrored from assistant message metadata). */
  tool_trace?: ToolTraceEntry[];
  /** True when the local LLM is unreachable and the server short-circuited
   *  with a friendly message instead of waiting for the connect timeout. */
  degraded?: boolean;
  llm_status?: {
    ok: boolean;
    model?: string;
    base_url?: string;
    reason?: string;
  };
  /** Per-iteration agent trace — only present when the user has dev_mode ON. */
  agent_trace?: AgentTrace;
}
