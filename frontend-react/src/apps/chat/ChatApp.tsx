/**
 * Yorik Chat — polished React port mirroring the WhatsApp / Email
 * three-pane shell. Left: conversation list. Center: bubble thread with
 * inline document cards when search_documents fires. Right: a "context"
 * pane that surfaces the latest SQL the agent ran (collapsible) plus the
 * non-document UI actions, so power users can see what the LLM did.
 *
 * The composer at the bottom sends through POST /api/ask and stitches
 * the server's reply (response + ui_actions) into the active thread.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  Loader2, Send, Plus, Search, Trash2, MessageSquare, Sparkles,
  FileText, Download, Eye, X, ArrowDown, ThumbsUp, ThumbsDown,
  AlertCircle, Globe, Check, Upload, Copy, RefreshCw, Calendar,
  CheckSquare, UsersRound, Cake, ChevronDown, ChevronLeft, ChevronRight, Wrench,
  Pin, PinOff, Mic, Pencil, Square, Bug,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Dock } from "@/components/Dock";
import { PendingActionChip } from "@/components/PendingActionChip";
import { emitUiAction } from "@/lib/uiActions";
import { NeedsInputCard, type NeedsInputAction } from "@/apps/compose/NeedsInputCard";
import { PhotoPickerCard, type PhotoPickerAction } from "@/apps/compose/PhotoPickerCard";
import { PeoplePickerCard, type PeoplePickerAction } from "@/components/PeoplePickerCard";
import { InlineComposeDraft } from "@/apps/chat/InlineComposeDraft";
import { AssistantMarkdown } from "@/components/AssistantMarkdown";
import { VcardImportModal } from "@/components/VcardImportModal";
import { MentionPopover, type MentionPick } from "./MentionPopover";
import {
  AttachmentStashTray, useAttachmentStash, type StashItem,
} from "./AttachmentStashTray";
import {
  useTriPane, MobileTopBar, MobileBackdrop,
  mobileAsideLeft,
} from "@/components/MobileShell";
import type {
  ConversationSummary, Conversation, ChatMessage, ToolTraceEntry,
  DocumentHit, PhotoHit, UiAction, AskResponse,
} from "./types";

// ---------------------------------------------------------------------------
// Root
// ---------------------------------------------------------------------------

export function ChatApp() {
  const { data: me } = useApi<{ user?: { id: number; name: string; first_name?: string; role: string } }>("/api/auth/me", []);
  const role = me?.user?.role || "admin";

  const convsApi = useApi<ConversationSummary[]>(`/api/conversations?role=${encodeURIComponent(role)}&limit=80`, [role]);
  const conversations = convsApi.data || [];

  const [activeId, setActiveId] = useState<string | null>(null);
  const [draftConversation, setDraftConversation] = useState<Conversation | null>(null);
  // Thread's `key` controls when React unmounts it. We bump this only
  // when explicitly switching between existing conversations (sidebar
  // click or "+ new") so that the draft → real-conversation transition
  // keeps the same component instance — preserving locally-attached
  // photos/documents/ui_actions that the server doesn't persist.
  const [threadKey, setThreadKey] = useState(0);

  // Deep-link: /chat?conversation_id=<id> opens that specific thread.
  // Used by the voice popover's "Continue in chat" button. Strips the
  // param from the URL after consuming so a refresh doesn't keep
  // re-pinning the same conversation if the user later navigates away.
  const location = useLocation();
  const [deepLinkConsumed, setDeepLinkConsumed] = useState(false);
  useEffect(() => {
    if (deepLinkConsumed) return;
    const params = new URLSearchParams(location.search);
    const wanted = params.get("conversation_id");
    if (!wanted) { setDeepLinkConsumed(true); return; }
    // Wait until the conversations list has loaded so we know if the
    // id exists; if it doesn't, fall back to default auto-select.
    if (!convsApi.data) return;
    const found = conversations.find(c => c.id === wanted);
    if (found) {
      setActiveId(wanted);
      setDraftConversation(null);
    }
    // Clear the param either way — don't want a stale id sticking
    // around in the URL bar.
    const nextUrl = location.pathname;
    window.history.replaceState({}, "", nextUrl);
    setDeepLinkConsumed(true);
  }, [location.search, location.pathname, convsApi.data, conversations, deepLinkConsumed]);

  // Auto-select most recent on first load — but skip when a deep-link
  // is still in flight, otherwise we'd flash the most-recent thread
  // for one render before swapping to the voiced one.
  useEffect(() => {
    if (!deepLinkConsumed) return;
    if (!activeId && !draftConversation && conversations.length > 0) {
      setActiveId(conversations[0].id);
    }
  }, [conversations, activeId, draftConversation, deepLinkConsumed]);

  function startNew() {
    setThreadKey(k => k + 1);  // fresh Thread for the new draft
    setActiveId(null);
    setDraftConversation({
      id: "",
      user_role: role,
      messages: [],
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
  }

  const tri = useTriPane();
  const activeConv = conversations.find(c => c.id === activeId);
  const headerTitle = draftConversation ? "New conversation"
    : activeConv?.preview?.slice(0, 30) || "Chat";

  return (
    <div className="flex h-screen bg-background text-foreground relative">
      <MobileBackdrop show={tri.leftOpen} onClick={tri.closeAll} />
      {/* ── Conversation list ────────────────────────────────── */}
      <aside className={cn(
        // Mobile: 85vw capped at 320px so a peek of the underlying
        // thread stays visible at the right edge (helps users orient
        // before tapping back to the chat). Desktop unchanged.
        "w-[min(85vw,320px)] md:w-[320px] border-r border-border flex flex-col bg-sidebar shrink-0",
        mobileAsideLeft(tri.leftOpen),
      )}>
        <header className="h-16 px-5 flex items-center justify-between border-b border-border">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-full bg-violet-500/15 flex items-center justify-center">
              <Sparkles className="w-4 h-4 text-violet-500" />
            </div>
            <div>
              <div className="font-semibold leading-none">Chat</div>
              <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5">
                {conversations.length} thread{conversations.length === 1 ? "" : "s"}
              </div>
            </div>
          </div>
          <button
            onClick={startNew}
            title="New conversation"
            className="w-8 h-8 inline-flex items-center justify-center rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition"
          >
            <Plus className="w-4 h-4" />
          </button>
        </header>

        <ConversationList
          conversations={conversations}
          activeId={activeId}
          draftActive={!!draftConversation && !activeId}
          loading={convsApi.loading}
          onSelect={(id) => {
            // Bump the threadKey so React unmounts the current Thread
            // and remounts it with the picked conversation's state.
            setThreadKey(k => k + 1);
            setActiveId(id);
            setDraftConversation(null);
          }}
          onDeleted={async () => {
            setThreadKey(k => k + 1);
            await convsApi.refetch();
            setActiveId(null);
          }}
          onChanged={async () => { await convsApi.refetch(); }}
          role={role}
        />

        <footer className="border-t border-border px-4 py-3 text-xs text-muted-foreground">
          {me?.user
            ? <>Logged in as <span className="text-foreground font-medium">{me.user.first_name || me.user.name.split(" ")[0] || me.user.name}</span> · {role}</>
            : "Loading…"}
        </footer>
      </aside>

      {/* ── Thread ──────────────────────────────────────────── */}
      <section className="flex-1 flex flex-col bg-background min-w-0 thread-bg">
        <MobileTopBar
          title={headerTitle}
          onMenuClick={() => tri.setLeftOpen(true)}
          onContextClick={() => tri.setRightOpen(true)}
          contextLabel="Context"
        />
        {(activeId || draftConversation) ? (
          <Thread
            key={threadKey}
            conversationId={activeId}
            role={role}
            draft={draftConversation}
            onConversationCreated={async (newId) => {
              setDraftConversation(null);
              setActiveId(newId);  // DO NOT bump threadKey — preserves local state
              await convsApi.refetch();
            }}
            onTurnAppended={() => { convsApi.refetch(); }}
          />
        ) : (
          <EmptyThread onStartNew={startNew} />
        )}
      </section>

      <Dock activeAppId="chat" />

      <style>{`
        .thread-bg {
          background-image:
            radial-gradient(circle at 30% 15%, hsl(263 70% 60% / 0.06), transparent 50%),
            radial-gradient(circle at 70% 85%, hsl(200 60% 60% / 0.05), transparent 50%);
        }
      `}</style>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Conversation list
// ---------------------------------------------------------------------------

function ConversationList({
  conversations, activeId, draftActive, loading, onSelect, onDeleted, onChanged, role,
}: {
  conversations: ConversationSummary[];
  activeId: string | null;
  draftActive: boolean;
  loading: boolean;
  onSelect: (id: string) => void;
  onDeleted: () => Promise<void> | void;
  /** Bumped after a pin toggle so the parent's list refetches. */
  onChanged: () => Promise<void> | void;
  role: string;
}) {
  const [filter, setFilter] = useState("");
  // Per-conversation debug-bundle modal — opened by the Bug icon in
  // each row's action cluster. null when closed.
  const [bundleFor, setBundleFor] = useState<ConversationSummary | null>(null);

  // Search filter — applied to title + preview so typed titles
  // (post auto-titling) are searchable too.
  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return conversations;
    return conversations.filter(c =>
      (c.title || "").toLowerCase().includes(q)
      || c.preview.toLowerCase().includes(q));
  }, [conversations, filter]);

  // Bucket conversations into Pinned + date groups. Pinned always
  // bubbles to its own section; everything else falls through to
  // Today / Yesterday / Last 7 days / Earlier based on updated_at.
  const grouped = useMemo(() => groupConversations(filtered), [filtered]);

  async function deleteConv(e: React.MouseEvent, id: string) {
    e.stopPropagation();
    if (!confirm("Delete this conversation?")) return;
    try {
      await api.delete(`/api/conversations/${encodeURIComponent(id)}?role=${encodeURIComponent(role)}`);
      await onDeleted();
    } catch (err: any) {
      alert("Delete failed: " + err.message);
    }
  }

  async function togglePin(e: React.MouseEvent, c: ConversationSummary) {
    e.stopPropagation();
    try {
      await api.post(
        `/api/conversations/${encodeURIComponent(c.id)}/pin`,
        { pinned: !c.pinned },
      );
      await onChanged();
    } catch (err: any) {
      alert("Pin failed: " + err.message);
    }
  }

  return (
    <>
      <div className="px-4 pt-3 pb-2">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
          <input
            value={filter}
            onChange={e => setFilter(e.target.value)}
            placeholder="Search conversations"
            className="w-full h-9 pl-9 pr-3 rounded-full bg-muted/70 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {draftActive && (
          <div className={cn(
            "w-full text-left px-3 py-2.5 mb-0.5 rounded-lg flex items-start gap-3",
            "bg-sidebar-accent shadow-sm",
          )}>
            <div className="w-9 h-9 rounded-full bg-violet-500/20 flex items-center justify-center shrink-0">
              <Sparkles className="w-4 h-4 text-violet-500" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-sm font-semibold">New conversation</div>
              <div className="text-xs text-muted-foreground italic">Start typing below…</div>
            </div>
          </div>
        )}

        {loading && conversations.length === 0 && (
          <div className="px-2 space-y-3 pt-2">
            {[1,2,3,4,5].map(i => (
              <div key={i} className="flex gap-3 p-2 animate-pulse">
                <div className="w-9 h-9 rounded-full bg-muted/60 shrink-0" />
                <div className="flex-1 space-y-2 pt-1">
                  <div className="h-3 bg-muted/60 rounded w-1/3" />
                  <div className="h-3 bg-muted/40 rounded w-5/6" />
                </div>
              </div>
            ))}
          </div>
        )}
        {!loading && filtered.length === 0 && !draftActive && (
          <div className="px-4 py-12 text-center text-xs text-muted-foreground">
            <MessageSquare className="w-8 h-8 mx-auto mb-3 opacity-30" />
            No conversations yet.<br/>Start one with the + button above.
          </div>
        )}
        {grouped.map(([groupLabel, items]) => (
          <div key={groupLabel} className="mt-2 first:mt-0">
            <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-muted-foreground/80 font-semibold flex items-center gap-1.5">
              {groupLabel === "Pinned" && <Pin className="w-2.5 h-2.5" />}
              {groupLabel}
            </div>
            {items.map(c => (
              <button
                key={c.id}
                onClick={() => onSelect(c.id)}
                className={cn(
                  "w-full text-left px-3 py-2.5 mb-0.5 rounded-lg flex items-start gap-3 transition group",
                  activeId === c.id ? "bg-sidebar-accent shadow-sm" : "hover:bg-sidebar-accent/50",
                )}
              >
                <div className="w-9 h-9 rounded-full bg-gradient-to-br from-violet-500/30 to-blue-500/30 flex items-center justify-center shrink-0">
                  <MessageSquare className="w-4 h-4 text-violet-500" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-sm truncate font-medium flex items-center gap-1.5">
                      {c.pinned && <Pin className="w-3 h-3 text-rose-500 shrink-0" />}
                      {c.title
                        || c.preview
                        || <span className="italic opacity-60">empty</span>}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-2 mt-0.5">
                    <span className="text-[11px] text-muted-foreground truncate flex-1 min-w-0">
                      {c.title && c.preview
                        ? <span className="italic opacity-80">{c.preview.slice(0, 50)}{c.preview.length > 50 ? "…" : ""}</span>
                        : `${c.message_count} message${c.message_count === 1 ? "" : "s"} · ${formatRelative(c.updated_at)}`}
                    </span>
                    {/* Pin + delete. Mobile: always visible at full
                        size (touch has no hover; without this they
                        were invisible AND too small to tap). Desktop:
                        hover-revealed to keep the list calm. */}
                    <div className="flex items-center gap-1 opacity-100 md:opacity-0 md:group-hover:opacity-100 transition">
                      <button
                        onClick={(e) => togglePin(e, c)}
                        className={cn(
                          "p-1.5 md:p-0 transition",
                          c.pinned
                            ? "text-rose-500"
                            : "text-muted-foreground hover:text-rose-500",
                        )}
                        title={c.pinned ? "Unpin from top" : "Pin to top"}
                        aria-label={c.pinned ? "Unpin conversation" : "Pin conversation"}
                      >
                        {c.pinned
                          ? <PinOff className="w-4 h-4 md:w-3 md:h-3" />
                          : <Pin className="w-4 h-4 md:w-3 md:h-3" />}
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setBundleFor(c); }}
                        className="p-1.5 md:p-0 text-muted-foreground hover:text-blue-500 transition"
                        title="Export debug bundle for bug report"
                        aria-label="Export debug bundle"
                      >
                        <Bug className="w-4 h-4 md:w-3 md:h-3" />
                      </button>
                      <button
                        onClick={(e) => deleteConv(e, c.id)}
                        className="p-1.5 md:p-0 text-muted-foreground hover:text-red-500 transition"
                        title="Delete conversation"
                        aria-label="Delete conversation"
                      >
                        <Trash2 className="w-4 h-4 md:w-3 md:h-3" />
                      </button>
                    </div>
                  </div>
                </div>
              </button>
            ))}
          </div>
        ))}
      </div>

      {bundleFor && (
        <DebugBundleModal
          conversation={bundleFor}
          onClose={() => setBundleFor(null)}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Thread + composer
// ---------------------------------------------------------------------------

function Thread({
  conversationId, role, draft, onConversationCreated, onTurnAppended,
}: {
  conversationId: string | null;
  role: string;
  draft: Conversation | null;
  onConversationCreated: (id: string) => void;
  onTurnAppended: () => void;
}) {
  // Per-conversation attachment stash. Empty array for the draft case
  // (no conversation row yet); becomes live the moment the first turn
  // creates the row. Photo/document cards call `addToStash` and pass
  // `inStash` to render their toggle state.
  const stash = useAttachmentStash(conversationId);
  // Either we're rendering an existing thread (loaded via GET), or a draft
  // that lives entirely in local state until the first turn lands.
  const loaded = useApi<Conversation>(
    conversationId ? `/api/conversations/${encodeURIComponent(conversationId)}?role=${encodeURIComponent(role)}` : null,
    [conversationId, role],
  );
  const [localMessages, setLocalMessages] = useState<ChatMessage[]>(draft?.messages || []);
  const [sending, setSending] = useState(false);
  // Live progress line shown under the typing indicator while the agent
  // loop streams iter_start / tool_start / tool_done events via SSE.
  // Cleared when the final event arrives. The chat shows e.g.
  // "🔍 Suche nach 'monkeytown braunschweig'…" or "📄 Lese monkeytown.eu…"
  // so the user knows what Yorik is actually doing.
  const [progress, setProgress] = useState<string | null>(null);
  // Streaming buffer for token-level assistant text. Filled by `text_delta`
  // SSE events; rendered as a pending assistant bubble. On `final`, the
  // buffer is cleared and the canonical message lands in localMessages
  // (which carries ui_actions, agent_trace, etc.).
  const [streamingText, setStreamingText] = useState<string>("");
  const [text, setText] = useState("");
  // Latched on /api/ask returning `degraded: true` or on a network error.
  // Cleared on the next successful turn. Drives the offline banner.
  const [llmOffline, setLlmOffline] = useState<{ reason?: string } | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  // Hidden file input — mobile paperclip button opens it. Drag-drop
  // remains the desktop path; both route through handleDroppedFile.
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  // AbortController for the in-flight /api/ask/stream fetch. Lets the
  // "Stop" button kill a generation mid-stream instead of forcing the
  // user to wait out a long answer.
  const streamAbortRef = useRef<AbortController | null>(null);
  // Per-message regenerate state — keyed by message index so a click
  // on bubble #4 only spins #4's button, not the whole thread.
  const [regenIdx, setRegenIdx] = useState<number | null>(null);
  // .vcf dropped onto the composer → open the shared VcardImportModal
  // pre-seeded with the file. Same backend, same preview/apply flow as
  // the Contacts page "Import .vcf" button.
  const [vcardDrop, setVcardDrop] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  // Inline voice-to-text (different from the global VoiceFab flow).
  // VoiceFab transcribes AND auto-sends to /api/ask-voice. The chat
  // composer's mic transcribes ONLY and drops the text into the input
  // box so the user can review + edit + hit Send themselves. Two flows
  // exist on purpose — power users like the speak-and-go behaviour
  // elsewhere; the chat input is for considered input.
  const [voiceState, setVoiceState] = useState<"idle" | "recording" | "transcribing">("idle");
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const [voiceSeconds, setVoiceSeconds] = useState(0);
  const voiceRecorderRef = useRef<MediaRecorder | null>(null);
  const voiceStreamRef = useRef<MediaStream | null>(null);
  const voiceChunksRef = useRef<Blob[]>([]);
  const voiceTickRef = useRef<number | null>(null);
  // Inline toast for non-vcf drops (PDF / image / .ics). Voice and
  // mention popovers don't need this; just file uploads do.
  const [uploadToast, setUploadToast] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // Mention/slash popover state — when null, no popover is open.
  // start/end mark the character range in the textarea that the
  // popover is currently editing (the trigger char + everything
  // typed after it up to the caret), so on pick we can splice.
  const [mentionState, setMentionState] = useState<{
    mode: "@" | "/";
    prefix: string;
    start: number;
    end: number;
  } | null>(null);

  // One-shot: if the onboarding wizard (or any other surface) stashed
  // a seed message via sessionStorage, drop it into the composer and
  // clear the slot so a refresh doesn't re-fire it.
  useEffect(() => {
    try {
      const seed = sessionStorage.getItem("yorik_chat_seed");
      if (seed) {
        sessionStorage.removeItem("yorik_chat_seed");
        setText(seed);
        // Skip auto-focus on touch devices — pops the soft keyboard
        // mid-load and jolts the layout. The user can tap to focus.
        const isTouch = typeof window !== "undefined"
          && window.matchMedia("(hover: none)").matches;
        if (!isTouch) {
          setTimeout(() => composerRef.current?.focus(), 50);
        }
      }
    } catch {}
  }, []);

  // When the conversation id changes (or finished loading), reset the
  // local buffer to the canonical server messages. Critically, the
  // server doesn't persist `documents` / `photos` / `ui_actions` —
  // those are extracted from the latest /api/ask response and live in
  // local state only. So for each server message that already has a
  // local twin with attached cards, preserve those attachments.
  useEffect(() => {
    if (conversationId && loaded.data) {
      const serverMsgs = loaded.data.messages || [];
      setLocalMessages(prevLocal => {
        // Walk server messages, find a matching local one (same role +
        // same content prefix) and carry over its photos/documents.
        // O(n) per side is fine — conversations rarely exceed a few
        // dozen turns.
        const used = new Set<number>();
        return serverMsgs.map(s => {
          for (let i = 0; i < prevLocal.length; i++) {
            if (used.has(i)) continue;
            const l = prevLocal[i];
            if (l.role !== s.role) continue;
            // Loose match: empty server content vs empty local OR
            // identical first ~60 chars (handles trailing punctuation
            // drift from server-side processing).
            const sc = (s.content || "").trim();
            const lc = (l.content || "").trim();
            const matches = sc === lc || (sc && lc && sc.slice(0, 60) === lc.slice(0, 60));
            if (!matches) continue;
            used.add(i);
            if (l.documents || l.photos || l.ui_actions) {
              return { ...s, documents: l.documents, photos: l.photos, ui_actions: l.ui_actions };
            }
            return s;
          }
          return s;
        });
      });
    } else if (!conversationId) {
      setLocalMessages(draft?.messages || []);
    }
  }, [conversationId, loaded.data, draft]);

  // When VoiceFab finishes a voice turn whose conversation_id matches
  // the one currently open in this chat, re-fetch from /api/
  // conversations/{id} so the new user-message + assistant-reply
  // appear inline. Without this, ping-pong voice turns land on the
  // server but never render in an already-open chat thread.
  useEffect(() => {
    if (!conversationId) return;
    function onVoiceTurnCompleted(ev: Event) {
      const detail = (ev as CustomEvent).detail || {};
      if (detail.conversation_id === conversationId) {
        loaded.refetch();
      }
    }
    window.addEventListener("yorik:voice:turn-completed", onVoiceTurnCompleted);
    return () => window.removeEventListener("yorik:voice:turn-completed", onVoiceTurnCompleted);
  }, [conversationId, loaded.refetch]);

  // Append the in-progress streaming bubble (if any) so the user sees
  // tokens appear in real time. On `final` SSE event we clear
  // streamingText and the real message lands in localMessages with
  // full metadata (ui_actions, agent_trace, etc.).
  const messages = streamingText
    ? [...localMessages, { role: "assistant" as const, content: streamingText }]
    : localMessages;

  // Auto-scroll on new message, unless user has scrolled up.
  useEffect(() => {
    if (!autoScroll) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, autoScroll]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setAutoScroll(nearBottom);
  }

  // Cards inside assistant bubbles (e.g. template picker) can ask the
  // composer to send a follow-up by dispatching this event. Avoids
  // threading a callback through every nested component.
  useEffect(() => {
    const handler = (e: Event) => {
      const seed = (e as CustomEvent<{ seed: string }>).detail?.seed;
      if (seed) {
        setText(seed);
        // Auto-send on the next tick.
        setTimeout(() => {
          (document.getElementById("yorik-chat-send-trigger") as HTMLButtonElement)?.click();
        }, 30);
      }
    };
    window.addEventListener("yorik:chat-seed-and-send", handler);
    return () => window.removeEventListener("yorik:chat-seed-and-send", handler);
  }, []);

  // Inline voice-to-text: records via MediaRecorder, POSTs the blob to
  // /api/voice/transcribe (Whisper, transcribe-only — no LLM call, no
  // TTS), and appends the transcript to the composer text. The user
  // then reviews + edits + clicks Send.
  const stopVoice = useCallback(async () => {
    const rec = voiceRecorderRef.current;
    if (!rec) return;
    if (rec.state !== "inactive") rec.stop();
  }, []);

  const handleVoiceStopped = useCallback(async (mimeType: string | undefined) => {
    if (voiceTickRef.current) { window.clearInterval(voiceTickRef.current); voiceTickRef.current = null; }
    const stream = voiceStreamRef.current;
    if (stream) stream.getTracks().forEach(t => t.stop());
    voiceStreamRef.current = null;

    const blob = new Blob(voiceChunksRef.current, { type: mimeType || "audio/webm" });
    voiceChunksRef.current = [];
    if (blob.size < 1000) {
      setVoiceError("Zu kurz — bitte noch mal aufnehmen.");
      setVoiceState("idle");
      return;
    }

    setVoiceState("transcribing");
    setVoiceError(null);
    try {
      const fd = new FormData();
      fd.append("audio", blob, "voice.webm");
      const r = await fetch("/api/voice/transcribe", { method: "POST", body: fd, credentials: "include" });
      if (!r.ok) {
        const j = await r.json().catch(() => ({} as any));
        throw new Error(j.detail || j.error || `HTTP ${r.status}`);
      }
      const data = await r.json() as { text: string };
      const transcript = (data.text || "").trim();
      if (!transcript) {
        setVoiceError("Leeres Transkript — bitte noch mal aufnehmen.");
        setVoiceState("idle");
        return;
      }
      // Append to existing text (with a space separator if there's
      // already content). Doesn't auto-send — user reviews + clicks Send.
      setText(prev => prev.trim() ? `${prev.trim()} ${transcript}` : transcript);
      setVoiceState("idle");
      // Re-focus composer so the user can edit / press Enter immediately.
      window.setTimeout(() => composerRef.current?.focus(), 30);
    } catch (e: any) {
      setVoiceError(e?.message || "Transkription fehlgeschlagen.");
      setVoiceState("idle");
    }
  }, []);

  const startVoice = useCallback(async () => {
    if (voiceState !== "idle") return;
    setVoiceError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      voiceStreamRef.current = stream;
      const rec = new MediaRecorder(stream);
      voiceRecorderRef.current = rec;
      voiceChunksRef.current = [];
      rec.ondataavailable = e => { if (e.data && e.data.size) voiceChunksRef.current.push(e.data); };
      rec.onstop = () => { void handleVoiceStopped(rec.mimeType); };
      rec.start();
      setVoiceState("recording");
      setVoiceSeconds(0);
      const startedAt = Date.now();
      voiceTickRef.current = window.setInterval(() => {
        setVoiceSeconds(Math.floor((Date.now() - startedAt) / 1000));
      }, 250);
    } catch (e: any) {
      setVoiceError(e?.message || "Kein Zugriff aufs Mikrofon.");
      setVoiceState("idle");
    }
  }, [voiceState, handleVoiceStopped]);

  // Cleanup on unmount — stop any in-flight recording + release stream.
  useEffect(() => {
    return () => {
      if (voiceTickRef.current) window.clearInterval(voiceTickRef.current);
      const stream = voiceStreamRef.current;
      if (stream) stream.getTracks().forEach(t => t.stop());
      const rec = voiceRecorderRef.current;
      if (rec && rec.state !== "inactive") {
        try { rec.stop(); } catch { /* ignore */ }
      }
    };
  }, []);

  const send = useCallback(async () => {
    const message = text.trim();
    if (!message || sending) return;
    setSending(true);
    setProgress("Yorik is thinking…");
    setStreamingText("");
    setText("");

    // Optimistically append the user turn so the bubble shows immediately.
    setLocalMessages(prev => [...prev, { role: "user", content: message }]);

    // Helper: take a final-event payload (same shape as the old /api/ask
    // response) and append the assistant turn + run side effects.
    function applyFinal(r: AskResponse) {
      const docs: DocumentHit[] = [];
      const photos: PhotoHit[] = [];
      const otherActions: UiAction[] = [];
      const dispatchableActions: UiAction[] = [];
      for (const a of r.ui_actions || []) {
        if (a.type === "documents_found" && Array.isArray((a as any).documents)) {
          for (const d of (a as any).documents as DocumentHit[]) {
            if (!docs.some(x => x.doc_id === d.doc_id)) docs.push(d);
          }
        } else if (a.type === "photos_found" && Array.isArray((a as any).photos)) {
          for (const p of (a as any).photos as PhotoHit[]) {
            if (!photos.some(x => x.id === p.id)) photos.push(p);
          }
        } else {
          otherActions.push(a);
          const STICKS_TO_MESSAGE = new Set([
            "pending_confirmation",
            "compose_draft_created",
            "template_picker",
            "pois_found",
            "contact_picker",
            "contacts_found",
            "tasks_found",
            "needs_input",
            "photo_picker",
            "people_picker",
            "web_results",
            "price_summary",
            "venue_saved",
          ]);
          if (!STICKS_TO_MESSAGE.has(a.type)) {
            dispatchableActions.push(a);
          }
        }
      }
      for (const a of dispatchableActions) emitUiAction(a);

      setLocalMessages(prev => [...prev, {
        role: "assistant",
        content: r.response || "(no response)",
        documents: docs.length > 0 ? docs : undefined,
        photos: photos.length > 0 ? photos : undefined,
        ui_actions: otherActions.length > 0 ? otherActions : undefined,
        tool_trace: r.tool_trace,
        sql_used: r.sql_used || undefined,
        agent_trace: r.agent_trace,
      }]);

      if (r.degraded) setLlmOffline({ reason: r.llm_status?.reason });
      else            setLlmOffline(null);

      if (!conversationId && r.conversation_id) onConversationCreated(r.conversation_id);
      else                                      onTurnAppended();
    }

    const ac = new AbortController();
    streamAbortRef.current = ac;
    try {
      const resp = await fetch("/api/ask/stream", {
        method:  "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body:    JSON.stringify({
          message, role, conversation_id: conversationId || undefined,
        }),
        signal:  ac.signal,
      });
      if (!resp.ok || !resp.body) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let gotFinal = false;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE events are separated by blank lines. Each event has
        // `data: <json>` lines; we only emit one data line per event.
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const chunk = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const dataLine = chunk.split("\n").find(l => l.startsWith("data: "));
          if (!dataLine) continue;
          let evt: any;
          try { evt = JSON.parse(dataLine.slice(6)); } catch { continue; }
          // Phase-driven UI updates.
          const phase = evt.phase as string | undefined;
          if (phase === "iter_start") {
            // Don't overwrite a tool-specific status with the generic
            // "thinking" — let the next tool_start refresh it.
            setProgress(prev => prev && prev !== "Yorik is thinking…" ? prev : "Yorik is thinking…");
          } else if (phase === "text_delta") {
            // Token-level streaming — append to the in-progress buffer.
            // The progress hint clears since the model is now producing
            // visible output; the bubble itself signals "alive".
            const delta = String(evt.text || "");
            if (delta) {
              setStreamingText(prev => prev + delta);
              setProgress(null);
            }
          } else if (phase === "tool_start") {
            setProgress(formatToolStatus(evt.tool, evt.args));
          } else if (phase === "tool_done") {
            // Show a brief "fertig" hint then idle the line so the next
            // tool_start replaces it. Many tools fire back-to-back so
            // we don't bother with a delay.
            setProgress(prev => prev?.startsWith("✓") ? prev : prev);
          } else if (phase === "final") {
            gotFinal = true;
            // Clear the streaming buffer before applyFinal pushes the
            // canonical message — avoids a double render in the same
            // frame (streaming bubble + final bubble).
            setStreamingText("");
            applyFinal(evt as AskResponse);
          } else if (phase === "error") {
            setLocalMessages(prev => [...prev, {
              role: "assistant",
              content: `_Yorik failed: ${evt.error}_`,
            }]);
          }
        }
      }
      if (!gotFinal) {
        setLocalMessages(prev => [...prev, {
          role: "assistant",
          content: "_Stream ended without a final response._",
        }]);
      }
    } catch (err: any) {
      // User-triggered abort isn't a failure — just stop quietly and
      // keep whatever text already streamed in as a partial bubble so
      // the work isn't lost.
      if (err?.name === "AbortError") {
        const partial = streamingText;
        if (partial && partial.trim()) {
          setLocalMessages(prev => [...prev, {
            role: "assistant",
            content: partial + "\n\n_(stopped)_",
          }]);
        }
      } else {
        setLlmOffline({ reason: err?.message || "network error" });
        setLocalMessages(prev => [...prev, {
          role: "assistant",
          content: `_Couldn't reach Yorik: ${err.message}_`,
        }]);
      }
    } finally {
      streamAbortRef.current = null;
      setSending(false);
      setProgress(null);
      setStreamingText("");
      // rAF defers focus() until AFTER the re-render that follows
      // setSending(false), so any focus-stealing condition (was
      // disabled, was conditionally rendered, etc.) has resolved by
      // the time we ask the textarea to focus. Without this the call
      // was a no-op when the textarea was still in the previous state.
      requestAnimationFrame(() => composerRef.current?.focus());
    }
  }, [text, sending, role, conversationId, onConversationCreated, onTurnAppended, streamingText]);

  function stopGeneration() {
    streamAbortRef.current?.abort();
  }

  // Per-message inline edit — local-only. Edits live in component
  // state; reloading the conversation from the server brings back the
  // LLM's original text. Intentional v1 scope: edits are personal
  // tweaks, not retroactive history rewrites.
  function editMessageContent(messageIdx: number, newContent: string) {
    setLocalMessages(prev => prev.map((m, i) =>
      i === messageIdx ? { ...m, content: newContent } : m
    ));
  }

  async function regenerateLast(messageIdx: number) {
    if (!conversationId || regenIdx !== null) return;
    setRegenIdx(messageIdx);
    try {
      const r = await api.post<AskResponse>(
        `/api/conversations/${encodeURIComponent(conversationId)}/regenerate`,
        {},
      );
      // Re-derive docs/photos/ui_actions the same way send() does.
      const docs: DocumentHit[] = [];
      const photos: PhotoHit[] = [];
      const otherActions: UiAction[] = [];
      for (const a of r.ui_actions || []) {
        if (a.type === "documents_found" && Array.isArray((a as any).documents)) {
          for (const d of (a as any).documents as DocumentHit[]) {
            if (!docs.some(x => x.doc_id === d.doc_id)) docs.push(d);
          }
        } else if (a.type === "photos_found" && Array.isArray((a as any).photos)) {
          for (const p of (a as any).photos as PhotoHit[]) {
            if (!photos.some(x => x.id === p.id)) photos.push(p);
          }
        } else {
          otherActions.push(a);
        }
      }
      // Replace the assistant message at messageIdx with the new one.
      setLocalMessages(prev => {
        const next = [...prev];
        next[messageIdx] = {
          role: "assistant",
          content: r.response || "(no response)",
          documents: docs.length > 0 ? docs : undefined,
          photos: photos.length > 0 ? photos : undefined,
          ui_actions: otherActions.length > 0 ? otherActions : undefined,
          tool_trace: r.tool_trace,
          sql_used: r.sql_used || undefined,
          agent_trace: r.agent_trace,
        };
        // Drop anything that was after this message (sanity — regenerate
        // truncates server-side too).
        return next.slice(0, messageIdx + 1);
      });
      onTurnAppended();
    } catch (err: any) {
      setLlmOffline({ reason: err?.message || "regenerate failed" });
    } finally {
      setRegenIdx(null);
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <>
      {/* MobileTopBar above already shows the conversation title on
          mobile, so this inner header is desktop-only — otherwise
          we'd eat 128px of chrome (2× h-16) before the first message
          on a phone. */}
      <header className="hidden md:flex h-16 px-6 border-b border-border items-center justify-between bg-background/80 backdrop-blur shrink-0">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-9 h-9 rounded-full bg-gradient-to-br from-violet-500/30 to-blue-500/30 flex items-center justify-center">
            <Sparkles className="w-4 h-4 text-violet-500" />
          </div>
          <div className="min-w-0">
            <div className="font-semibold truncate">
              {conversationId
                ? (loaded.data?.title
                    || loaded.data?.messages?.[0]?.content?.slice(0, 60)
                    || "Conversation")
                : "New conversation"}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {messages.length === 0
                ? "Ask me anything — calendar, docs, tasks…"
                : `${messages.length} message${messages.length === 1 ? "" : "s"}`}
            </div>
          </div>
        </div>
      </header>

      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto px-6 py-6"
      >
        {/* Centred reading column — matches the ChatGPT/Grok layout so
            the chat doesn't stretch edge-to-edge on a wide window.
            Outer div keeps scroll + bg full-width; this inner wrapper
            caps the message column at ~768px and centres it. */}
        <div className="max-w-3xl mx-auto w-full space-y-1">
          {loaded.loading && messages.length === 0 && (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="w-5 h-5 animate-spin" />
            </div>
          )}
          {!loaded.loading && messages.length === 0 && (
            <TodayDigest onPick={(s) => { setText(s); composerRef.current?.focus(); }} />
          )}
          {(() => {
            // Render-time dedup of repeat document cards. The LLM calls
            // search_documents on every content-extraction follow-up
            // ("wie hoch ist der Betrag", "wann ist es fällig?") which
            // floods the chat with the SAME doc grid the user already
            // saw on turn 1. Filter each message's documents against
            // ones already rendered earlier in this conversation; if
            // nothing fresh remains, the card grid is suppressed but
            // the reply text still shows.
            const seenDocIds = new Set<string | number>();
            return messages
              .filter(m => {
                if ((m.role as string) === "tool") return false;
                if (m.role === "assistant"
                    && !m.content?.trim()
                    && !m.documents?.length
                    && !m.photos?.length
                    && !m.ui_actions?.length) return false;
                return true;
              })
              .map(m => {
                if (!m.documents?.length) return m;
                const fresh = m.documents.filter(d => {
                  const id = (d as any).doc_id ?? (d as any).id;
                  return id != null && !seenDocIds.has(id);
                });
                m.documents.forEach(d => {
                  const id = (d as any).doc_id ?? (d as any).id;
                  if (id != null) seenDocIds.add(id);
                });
                return fresh.length === m.documents.length ? m : { ...m, documents: fresh };
              })
              .map((m, i, arr) => (
            <MessageBubble
              key={i}
              message={m}
              isLast={i === arr.length - 1}
              role={role}
              conversationId={conversationId}
              messageIdx={i}
              onRegenerate={conversationId ? () => regenerateLast(i) : undefined}
              regenBusy={regenIdx === i}
              regenDisabled={regenIdx !== null}
              onEditContent={(newContent) => editMessageContent(i, newContent)}
              onAttach={stash.add}
              isAttached={stash.has}
            />
          ));
          })()}
          {sending && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground pl-10 md:pl-12 pt-2">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              <span>{progress || "Yorik is thinking…"}</span>
              {/* Stop button lives in the send-button slot now — a fixed
                  target you can find without chasing the moving status
                  text. See the composer below. */}
            </div>
          )}
        </div>
      </div>

      {!autoScroll && messages.length > 4 && (
        <button
          onClick={() => {
            setAutoScroll(true);
            const el = scrollRef.current;
            if (el) el.scrollTop = el.scrollHeight;
          }}
          className="absolute bottom-24 right-1/2 translate-x-1/2 w-9 h-9 rounded-full bg-card border border-border shadow-md hover:bg-muted text-muted-foreground flex items-center justify-center"
          title="Scroll to latest"
          style={{ position: "relative" }}
        >
          <ArrowDown className="w-4 h-4" />
        </button>
      )}

      {/* LLM-offline banner */}
      {llmOffline && (
        <div className="mx-6 mt-2 -mb-1 px-3 py-2 rounded-md bg-amber-500/10 border border-amber-500/30 text-amber-700 dark:text-amber-400 text-xs flex items-start gap-2">
          <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="font-medium">Yorik's brain is offline</div>
            <div className="opacity-80 mt-0.5 leading-relaxed">
              {llmOffline.reason
                ? <>The local LLM endpoint isn't responding ({llmOffline.reason}). Start it and your messages will go through again.</>
                : "The local LLM endpoint isn't responding. Start it and your messages will go through again."}
            </div>
          </div>
          <button
            onClick={() => setLlmOffline(null)}
            className="text-amber-700/60 hover:text-amber-700 dark:text-amber-400/60 dark:hover:text-amber-400 shrink-0"
            title="Dismiss"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {/* Composer — outer div spans full width so the top border + bg
          backdrop reach the edges; inner wrapper is capped + centred
          to line up with the message column above. Drop zone routes
          by file type: .vcf → contact import modal, PDF/image →
          /api/documents/upload, .ics → calendar import (TODO). */}
      <div
        // pb on mobile uses safe-area-inset-bottom so the composer
        // clears the iOS home-indicator gesture zone (~34px on
        // iPhones with the bar). Desktop keeps its pb-20 (room for
        // the Dock).
        className={cn(
          "px-3 md:px-6 pt-3 pb-[max(5rem,calc(env(safe-area-inset-bottom)+1rem))] md:pb-20 border-t border-border bg-background/80 backdrop-blur transition",
          dragOver && "bg-amber-500/10",
        )}
        onDragOver={e => {
          if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
            e.preventDefault();
            setDragOver(true);
          }
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={e => {
          if (!e.dataTransfer?.files?.length) return;
          e.preventDefault();
          setDragOver(false);
          handleDroppedFile(e.dataTransfer.files[0]);
        }}
      >
        <div className="max-w-3xl mx-auto w-full relative">
          {dragOver && (
            <div className="text-[11px] text-amber-700 dark:text-amber-400 text-center mb-2 flex items-center justify-center gap-1.5">
              <Upload className="w-3.5 h-3.5" /> Drop here — .vcf, .pdf, image, or .ics
            </div>
          )}
          {uploadToast && (
            <div className={cn(
              "absolute -top-10 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-full text-[11px] flex items-center gap-1.5 shadow-md border",
              uploadToast.kind === "ok"
                ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/30"
                : "bg-rose-500/10 text-rose-700 dark:text-rose-400 border-rose-500/30",
            )}>
              {uploadToast.kind === "ok" ? <Check className="w-3 h-3" /> : <AlertCircle className="w-3 h-3" />}
              <span>{uploadToast.text}</span>
            </div>
          )}
          {/* Voice status — only visible while the inline mic is active.
              Recording shows a red pulsing dot + seconds counter; the
              transcribing state shows a quiet "transcribing…" line; any
              error renders as a dismissible bar above the composer. */}
          {voiceState === "recording" && (
            <div className="text-[11px] text-red-600 text-center mb-2 flex items-center justify-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
              Recording — {voiceSeconds}s — tap the mic again to insert the text
            </div>
          )}
          {voiceState === "transcribing" && (
            <div className="text-[11px] text-muted-foreground text-center mb-2 flex items-center justify-center gap-1.5">
              <Loader2 className="w-3 h-3 animate-spin" />
              Transcribing…
            </div>
          )}
          {voiceError && voiceState === "idle" && (
            <div className="text-[11px] text-rose-600 text-center mb-2 flex items-center justify-center gap-1.5">
              <AlertCircle className="w-3 h-3" />
              {voiceError}
              <button
                type="button"
                onClick={() => setVoiceError(null)}
                className="underline ml-1"
              >
                ok
              </button>
            </div>
          )}
          <AttachmentStashTray
            items={stash.items}
            onRemove={stash.remove}
            onClear={stash.clear}
          />
          {/* Mention / slash popover sits above the composer, anchored
              to the inner wrapper so it stays width-matched. */}
          {mentionState && (
            <MentionPopover
              mode={mentionState.mode}
              prefix={mentionState.prefix}
              onPick={(pick) => handleMentionPick(pick)}
              onCancel={() => setMentionState(null)}
            />
          )}
          <div className="flex items-end gap-1 md:gap-2 bg-muted/60 rounded-3xl pl-4 md:pl-5 pr-2 py-2 focus-within:bg-muted focus-within:ring-2 focus-within:ring-ring/30 transition">
            <textarea
              ref={composerRef}
              value={text}
              onChange={e => {
                setText(e.target.value);
                detectMentionTrigger(e.target.value, e.target.selectionStart ?? 0);
                // Real auto-resize: measure actual rendered height
                // including soft-wraps, not just newlines. The previous
                // newline-count heuristic kept long single-line text
                // pinned at 24px while wrapping invisibly.
                const el = e.currentTarget;
                el.style.height = "auto";
                el.style.height = Math.min(160, Math.max(40, el.scrollHeight)) + "px";
              }}
              onKeyDown={onKeyDown}
              onSelect={e => {
                const t = e.currentTarget;
                detectMentionTrigger(t.value, t.selectionStart ?? 0);
              }}
              rows={1}
              placeholder="Ask me anything…   try @hans or /event"
              // Intentionally NOT disabled during `sending`. Disabling
              // the focused textarea would auto-blur it (browser rule),
              // which is what the user kept noticing as "I get focused
              // out of chat." The onKeyDown handler short-circuits
              // Enter while sending=true (see `send()` early-return) so
              // double-sends are already prevented; users can keep
              // typing their next message during the stream and hit
              // Enter once it completes.
              className="flex-1 bg-transparent resize-none focus:outline-none text-sm max-h-40 py-2 min-h-[40px] md:min-h-0"
            />
            {/* Paperclip — file upload entry on mobile (drag-drop is
                desktop-only since touch has no drag). The hidden
                input routes through handleDroppedFile which already
                does the .vcf vs .pdf vs image routing. */}
            <input
              ref={fileInputRef}
              type="file"
              accept=".vcf,.pdf,.ics,image/*"
              className="hidden"
              onChange={e => {
                const f = e.target.files?.[0];
                if (f) handleDroppedFile(f);
                // Reset so picking the same file twice still fires onChange.
                e.target.value = "";
              }}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={sending}
              title="Attach a file"
              aria-label="Attach a file"
              className="md:hidden w-11 h-11 rounded-full flex items-center justify-center shrink-0 text-muted-foreground hover:text-foreground hover:bg-muted/60 transition disabled:opacity-50"
            >
              <Upload className="w-5 h-5" />
            </button>
            <button
              type="button"
              onClick={voiceState === "recording" ? stopVoice : startVoice}
              disabled={sending || voiceState === "transcribing"}
              title={
                voiceState === "recording" ? `Stop and insert into the input (${voiceSeconds}s)`
                : voiceState === "transcribing" ? "Transcribing…"
                : "Speak — text lands in the input for editing"
              }
              aria-label={voiceState === "recording" ? "Stop recording" : "Start voice-to-text"}
              className={cn(
                "w-11 h-11 md:w-9 md:h-9 rounded-full flex items-center justify-center shrink-0 transition disabled:opacity-50",
                voiceState === "recording"
                  ? "bg-red-500/20 text-red-600 hover:bg-red-500/30 animate-pulse"
                  : "text-muted-foreground hover:text-foreground hover:bg-muted/60",
              )}
            >
              {voiceState === "transcribing"
                ? <Loader2 className="w-5 h-5 md:w-4 md:h-4 animate-spin" />
                : voiceState === "recording"
                  ? <Square className="w-4 h-4 md:w-3.5 md:h-3.5 fill-current" />
                  : <Mic className="w-5 h-5 md:w-4 md:h-4" />}
            </button>
            {/* Send / Stop — the single right-edge button. While Yorik
                is generating, this becomes a Stop button (same spot,
                same shape) so the user always has a fixed click target
                instead of chasing the status line above. */}
            <button
              id="yorik-chat-send-trigger"
              onClick={sending ? stopGeneration : send}
              disabled={!sending && !text.trim()}
              title={sending ? "Stop generating (cancel this turn)" : "Send (Enter)"}
              aria-label={sending ? "Stop generating" : "Send message"}
              className={cn(
                "w-11 h-11 md:w-9 md:h-9 rounded-full flex items-center justify-center shrink-0 transition",
                sending
                  ? "bg-rose-500 hover:bg-rose-600 text-white shadow-md"
                  : text.trim()
                    ? "bg-violet-500 hover:bg-violet-600 text-white shadow-md"
                    : "bg-muted-foreground/20 text-muted-foreground cursor-not-allowed",
              )}
            >
              {sending
                ? <Square className="w-4 h-4 md:w-3.5 md:h-3.5 fill-current" />
                : <Send className="w-5 h-5 md:w-4 md:h-4" />}
            </button>
          </div>
          {/* Keyboard-shortcut hint hidden on mobile — there's no
              Enter/Shift+Enter on a phone soft keyboard, and drag-drop
              isn't available. The line just ate one composer's worth
              of vertical space for noise. */}
          <div className="hidden md:block text-[10px] text-muted-foreground mt-1.5 text-center">
            Enter to send · Shift+Enter newline · @ mention · / commands · drop files
          </div>
        </div>
      </div>

      {vcardDrop && (
        <VcardImportModal
          initialFile={vcardDrop}
          onClose={() => setVcardDrop(null)}
          onApplied={() => setVcardDrop(null)}
        />
      )}
    </>
  );

  /**
   * Dispatch a dropped file by type: vCard → existing import modal,
   * PDF/image/Office → /api/documents/upload (lands in the library +
   * Paperless via the existing write-through), .ics → calendar
   * import (TODO — show "not yet supported" hint until the endpoint
   * lands).
   */
  function handleDroppedFile(file: File) {
    const name = file.name.toLowerCase();
    const isVcf = /\.vcf$/i.test(name)
               || file.type === "text/vcard"
               || file.type === "text/x-vcard";
    const isPdf = /\.pdf$/i.test(name) || file.type === "application/pdf";
    const isDoc = /\.(docx?|odt|rtf)$/i.test(name);
    const isImg = file.type.startsWith("image/") || /\.(png|jpg|jpeg|heic|webp|gif)$/i.test(name);
    const isIcs = /\.ics$/i.test(name) || file.type === "text/calendar";

    if (isVcf) { setVcardDrop(file); return; }
    if (isIcs) {
      setUploadToast({ kind: "err", text: "ICS import isn't wired yet — open the file manually in your calendar." });
      window.setTimeout(() => setUploadToast(null), 4000);
      return;
    }
    if (isPdf || isDoc || isImg) {
      void uploadDocument(file);
      return;
    }
    setUploadToast({
      kind: "err",
      text: `${file.name}: unsupported type for chat drop`,
    });
    window.setTimeout(() => setUploadToast(null), 3500);
  }

  async function uploadDocument(file: File) {
    setUploadToast({ kind: "ok", text: `Uploading ${file.name}…` });
    try {
      const fd = new FormData();
      fd.append("file", file);
      // The existing endpoint /api/documents/upload handles MIME +
      // role acl; no extra metadata required for an ad-hoc chat drop.
      const r = await fetch("/api/documents/upload", {
        method: "POST",
        credentials: "include",
        body: fd,
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setUploadToast({ kind: "ok", text: `Added to your library: ${file.name}` });
    } catch (err: any) {
      setUploadToast({
        kind: "err",
        text: `Upload failed: ${err?.message || err}`,
      });
    } finally {
      window.setTimeout(() => setUploadToast(null), 4500);
    }
  }

  /**
   * Watch the textarea for `@` or `/` typed at a word boundary and
   * open the mention popover. Word boundary = start of input OR
   * preceded by whitespace. Trigger is closed when:
   *   - caret moves out of the trigger run
   *   - the user deletes the trigger char
   *   - the user presses Escape (handled inside MentionPopover)
   *   - the user types a space (breaks the run)
   */
  function detectMentionTrigger(value: string, caret: number) {
    // Walk backwards from the caret to the most recent whitespace
    // (or start of input). The "run" between that boundary and the
    // caret is what we examine.
    let i = caret - 1;
    while (i >= 0 && !/\s/.test(value[i])) i--;
    const runStart = i + 1;
    const run = value.slice(runStart, caret);
    if (run.length === 0) {
      if (mentionState) setMentionState(null);
      return;
    }
    const first = run[0];
    if (first !== "@" && first !== "/") {
      if (mentionState) setMentionState(null);
      return;
    }
    // Slash commands only fire when the run starts the message —
    // otherwise "https://" trips them.
    if (first === "/" && runStart !== 0) {
      if (mentionState) setMentionState(null);
      return;
    }
    setMentionState({
      mode: first,
      prefix: run.slice(1),
      start: runStart,
      end: caret,
    });
  }

  function handleMentionPick(pick: MentionPick) {
    const s = mentionState;
    setMentionState(null);
    if (!s) return;

    // Slash command with `fullMessage` REPLACES the composer entirely
    // and auto-sends — the user picked a one-shot intent.
    if (pick.fullMessage) {
      setText(pick.fullMessage);
      // Defer to next tick so React paints the textarea update first.
      window.setTimeout(() => {
        (document.getElementById("yorik-chat-send-trigger") as HTMLButtonElement)?.click();
      }, 30);
      return;
    }
    // Slash command with a `tag` template → seed composer prefix and
    // place the caret at the end so the user types the rest.
    // @-mention → splice `@Name [tag]` over the trigger run.
    const before = text.slice(0, s.start);
    const after  = text.slice(s.end);
    const insertion = pick.displayText + (pick.tag || "") + " ";
    const next = before + insertion + after;
    setText(next);
    window.setTimeout(() => {
      const el = composerRef.current;
      if (el) {
        const caret = before.length + insertion.length;
        el.focus();
        el.setSelectionRange(caret, caret);
      }
    }, 0);
  }
}

// ---------------------------------------------------------------------------
// Bubble
// ---------------------------------------------------------------------------

function MessageBubble({
  message, isLast, role, conversationId, messageIdx,
  onRegenerate, regenBusy, regenDisabled, onEditContent,
  onAttach, isAttached,
}: {
  message: ChatMessage;
  isLast: boolean;
  role: string;
  conversationId: string | null;
  messageIdx: number;
  onRegenerate?: () => void;
  regenBusy?: boolean;
  regenDisabled?: boolean;
  onEditContent?: (newContent: string) => void;
  onAttach?: (item: StashItem) => void;
  isAttached?: (url: string, filename: string) => boolean;
}) {
  const isUser = message.role === "user";
  const [copied, setCopied] = useState(false);
  // Inline edit — local to the bubble. Save commits via onEditContent
  // up to ChatApp's message state; cancel discards the draft.
  const [editing, setEditing] = useState(false);
  const [editDraft, setEditDraft] = useState(message.content || "");
  async function copyContent() {
    try {
      await navigator.clipboard.writeText(message.content || "");
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // ignore — surface in toast if needed
    }
  }
  function startEdit() {
    setEditDraft(message.content || "");
    setEditing(true);
  }
  function saveEdit() {
    onEditContent?.(editDraft);
    setEditing(false);
  }
  function cancelEdit() {
    setEditDraft(message.content || "");
    setEditing(false);
  }
  return (
    <div className={cn("flex gap-3 group", isUser ? "flex-row-reverse" : "flex-row", isLast && "pb-2")}>
      <div className={cn(
        "w-7 h-7 md:w-9 md:h-9 rounded-full shrink-0 flex items-center justify-center mt-0.5",
        isUser
          ? "bg-gradient-to-br from-blue-500 to-violet-500 text-white"
          : "bg-gradient-to-br from-violet-500/30 to-blue-500/30",
      )}>
        {isUser
          ? <span className="text-xs font-semibold">You</span>
          : <Sparkles className="w-4 h-4 text-violet-500" />}
      </div>
      <div className={cn(
        // Mobile: bubbles fill the available width (minus avatar +
        // gap). At 375px viewport this gives ~270px of text width
        // vs the old 174px — readable paragraphs instead of 4-word
        // wraps. Desktop keeps the conversational 68% cap.
        "max-w-[88%] md:max-w-[68%] min-w-0",
        isUser && "items-end flex flex-col",
      )}>
        <div className={cn(
          "rounded-2xl px-4 py-2.5 text-sm leading-relaxed break-words",
          // User messages: preserve their literal whitespace (they typed
          // newlines, they meant newlines). Assistant: let markdown
          // handle paragraph spacing — pre-wrap would double-space lists.
          isUser
            ? "bg-violet-500 text-white rounded-tr-md whitespace-pre-wrap"
            : "bg-card border border-border rounded-tl-md",
        )}>
          {editing && !isUser ? (
            <div className="space-y-2">
              <textarea
                value={editDraft}
                onChange={(e) => setEditDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") { e.preventDefault(); cancelEdit(); }
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault(); saveEdit();
                  }
                }}
                rows={Math.min(20, Math.max(3, editDraft.split("\n").length + 1))}
                autoFocus
                className="w-full bg-background border border-border rounded-md p-2 text-sm leading-relaxed focus:outline-none focus:ring-2 focus:ring-violet-500/40 resize-y min-w-[280px]"
              />
              <div className="flex items-center gap-2 text-[11px]">
                <button
                  type="button"
                  onClick={saveEdit}
                  className="px-2 py-1 rounded bg-violet-500 text-white hover:bg-violet-600 transition"
                  title="Save (Cmd/Ctrl+Enter)"
                >
                  Save
                </button>
                <button
                  type="button"
                  onClick={cancelEdit}
                  className="px-2 py-1 rounded border border-border hover:bg-muted transition"
                  title="Discard changes (Esc)"
                >
                  Cancel
                </button>
                <span className="text-muted-foreground ml-1">
                  edits stay local — they don't change the LLM's history
                </span>
              </div>
            </div>
          ) : isUser
            ? message.content
            : <AssistantMarkdown>{message.content}</AssistantMarkdown>}
        </div>

        {/* Tool-trace summary — one-line ambient hint of what tools
            ran for this turn, shown on every assistant bubble that
            invoked at least one tool. Click expands to args + result
            details. */}
        {!isUser && message.tool_trace && message.tool_trace.length > 0 && (
          <ToolTraceSummary entries={message.tool_trace} />
        )}

        {/* Per-message actions — copy, edit, regenerate. Visible on
            hover so they don't clutter the resting thread. Regenerate
            only on the LAST assistant message (re-running an older
            turn would orphan everything after it). Edit lets you tweak
            the assistant's text inline — useful for cleaning up an
            LLM draft before copying it elsewhere. Hidden while in
            edit mode since the textarea has its own Save/Cancel. */}
        {!isUser && !editing && (
          <div className={cn(
            "mt-1 flex items-center gap-1 transition",
            // Mobile: always visible (no hover state on touch — without
            // this, mobile users couldn't copy / edit / regenerate
            // without manually selecting text). Desktop: hover-revealed
            // to keep the resting thread visually calm.
            "opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus-within:opacity-100",
          )}>
            <button
              type="button"
              onClick={copyContent}
              className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 transition"
              title="Copy reply"
            >
              {copied
                ? <><Check className="w-3 h-3 text-emerald-500" /> copied</>
                : <><Copy className="w-3 h-3" /> copy</>}
            </button>
            {onEditContent && (
              <button
                type="button"
                onClick={startEdit}
                className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 transition"
                title="Edit this message inline (local only)"
              >
                <Pencil className="w-3 h-3" /> edit
              </button>
            )}
            {isLast && onRegenerate && conversationId && (
              <button
                type="button"
                onClick={onRegenerate}
                disabled={regenDisabled}
                className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted/50 transition disabled:opacity-50"
                title="Try another answer (re-runs from the previous user message)"
              >
                {regenBusy
                  ? <><Loader2 className="w-3 h-3 animate-spin" /> regenerating…</>
                  : <><RefreshCw className="w-3 h-3" /> regenerate</>}
              </button>
            )}
          </div>
        )}

        {/* Document cards (assistant only) */}
        {!isUser && message.documents && message.documents.length > 0 && (
          <div className={cn(
            "mt-2 grid gap-2",
            message.documents.length === 1 ? "grid-cols-1" : "grid-cols-1 sm:grid-cols-2",
          )}>
            {message.documents.map(d => (
              <DocumentResultCard
                key={d.doc_id}
                doc={d}
                role={role}
                onAttach={onAttach}
                isAttached={isAttached}
              />
            ))}
          </div>
        )}
        {/* Photo grid (assistant only) — Immich thumbnails for find_photo
            results. URLs hit Yorik's /api/photos/{id}/thumbnail proxy,
            which forwards with the calling user's per-user Immich key
            server-side. The browser never sees the key. Click → open
            in Photos app. */}
        {!isUser && message.photos && message.photos.length > 0 && (
          <PhotoResultGrid
            photos={message.photos}
            onAttach={onAttach}
            isAttached={isAttached}
          />
        )}
        {/* Pending confirmation panel — when the LLM created/updated/
            deleted something and confirm_mutations is ON. Inline panel
            with 3 buttons; resolves the action via /api/pending/{id}/*. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "pending_confirmation")
          .map((a: any) => (
            <PendingActionChip
              key={a.pending_id}
              action={{
                pending_id: a.pending_id,
                skill:      a.skill,
                preview:    a.preview,
                llm_model:  a.llm_model,
              }}
            />
          ))}
        {/* Compose-draft cards — the LLM called compose_draft skill;
            this is the magical inline experience: TipTap editor in
            chat, refine via LLM with one input field, recipient picker
            + send-method radio + Send button all inline. No need to
            leave the chat unless you want the bigger Compose toolbar
            (link in the card header). */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "compose_draft_created")
          .map((a: any) => (
            <InlineComposeDraft
              key={a.draft_id}
              draftId={a.draft_id}
              kind={a.kind}
              recipient={a.recipient}
              subject={a.subject}
              preview={a.preview}
              templateId={a.template_id}
              templateName={a.template_name}
              missingArgs={a.missing_args || []}
            />
          ))}
        {/* Template picker — the LLM called compose_draft with a vague
            body, so the skill emitted picker candidates instead of a
            draft. User clicks one → it sends a follow-up chat message
            telling the LLM to draft with that template. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "template_picker")
          .map((a: any, i: number) => (
            <TemplatePickerCard
              key={i}
              query={a.query || a.intent || ""}
              templates={a.templates || []}
            />
          ))}
        {/* POI picker — find_provider_nearby returned a list of nearby
            practices. Render as a searchable card so the user can scan +
            pick one (with a clear "none of these" escape hatch for the
            case where the user already has their own provider in mind). */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "pois_found")
          .map((a: any, i: number) => (
            <PoiPickerCard
              key={i}
              poi={a.poi}
              near={a.near}
              pois={a.pois || []}
            />
          ))}
        {/* Contact picker — find_contact returned >1 candidate. Render
            click-to-pick cards instead of forcing the user to type the
            name back. Click → seed a follow-up message with the
            contact_id so the LLM resolves cleanly. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "contact_picker")
          .map((a: any, i: number) => (
            <ContactPickerCard
              key={i}
              query={a.query}
              contacts={a.contacts || []}
              ranked={!!a.ranked}
            />
          ))}
        {/* Tasks list — check_tasks emits this so questions like "welche
            Aufgaben sind überfällig" render as clickable rows (mark done
            inline, or click → opens /tasks?task=ID highlighted) instead
            of a static markdown bullet list. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "tasks_found")
          .map((a: any, i: number) => (
            <TasksFoundCard
              key={i}
              tasks={a.tasks || []}
              total={typeof a.total === "number" ? a.total : (a.tasks || []).length}
            />
          ))}
        {/* Calendar events — check_calendar emits this so "what's on today?"
            renders as inline clickable rows (open → /calendar?event=ID)
            instead of forcing the user to switch apps. Sister to tasks_found. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "events_found")
          .map((a: any, i: number) => (
            <EventsFoundCard
              key={i}
              events={a.events || []}
              total={typeof a.total === "number" ? a.total : (a.events || []).length}
            />
          ))}
        {/* Contacts list — list_contacts_for_picking emits this so
            questions like "zeig mir die pending Kontakte" render as a
            searchable, clickable list (click → opens /contacts?contact=ID)
            instead of a static markdown list. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "contacts_found")
          .map((a: any, i: number) => (
            <ContactsFoundCard
              key={i}
              contacts={a.contacts || []}
              total={typeof a.total === "number" ? a.total : (a.contacts || []).length}
              filter={a.filter || null}
              pickToChat={Boolean(a.pick_to_chat)}
            />
          ))}
        {/* Needs-input form — emitted by compose_draft / compose_check_recipient
            / find_recipient_address_from_documents when required template
            args are missing. Renders an inline form; on submit, the
            synthesised resume_message is fired back as a chat turn so the
            LLM playbook resumes (→ compose_check_recipient → compose_draft). */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "needs_input")
          .map((a: any, i: number) => (
            <NeedsInputCard
              key={i}
              action={a as NeedsInputAction}
              onSubmit={(resumeMessage: string) => {
                try { sessionStorage.setItem("yorik_chat_seed", resumeMessage); } catch {}
                window.dispatchEvent(new CustomEvent("yorik:chat-seed-and-send", { detail: { seed: resumeMessage } }));
              }}
              toast={(text, kind) => {
                // Main chat doesn't have a toast system; surface form-save
                // errors to the console so they're not silently lost.
                if (kind === "error") console.error("[needs_input]", text);
                else console.log("[needs_input]", text);
              }}
            />
          ))}
        {/* Photo picker — propose_inline_photo skill emits this when the
            user asked for an inline image in a Compose draft ("packe ein
            foto herein"). Same resume-message handoff as needs_input:
            user picks a thumbnail → resume_message goes back to the LLM
            playbook, which calls compose_draft with inline_image_url. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "photo_picker")
          .map((a: any, i: number) => (
            <PhotoPickerCard
              key={i}
              action={a as PhotoPickerAction}
              onSubmit={(resumeMessage: string) => {
                try { sessionStorage.setItem("yorik_chat_seed", resumeMessage); } catch {}
                window.dispatchEvent(new CustomEvent("yorik:chat-seed-and-send", { detail: { seed: resumeMessage } }));
              }}
            />
          ))}
        {/* People picker — find_photo emits this when the user asked
            for someone whose face isn't labeled in Immich yet (e.g.
            "foto von anna" but Immich has no "Sara"). User taps a
            face thumbnail → backend PUTs the label to Immich → resume
            message re-runs find_photo with the same args, and the
            newly-labeled face now resolves cleanly. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "people_picker")
          .map((a: any, i: number) => (
            <PeoplePickerCard
              key={i}
              action={a as PeoplePickerAction}
              onSubmit={(resumeMessage: string) => {
                try { sessionStorage.setItem("yorik_chat_seed", resumeMessage); } catch {}
                window.dispatchEvent(new CustomEvent("yorik:chat-seed-and-send", { detail: { seed: resumeMessage } }));
              }}
            />
          ))}
        {/* Web-search results — title + url + snippet cards. Clicking a
            result triggers a follow-up message asking the LLM to web_fetch
            that URL with a sensible question. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "web_results")
          .map((a: any, i: number) => (
            <WebResultsCard
              key={i}
              query={a.query}
              provider={a.provider}
              results={a.results || []}
            />
          ))}
        {/* Receipt-style price summary — compute_group_price emitted
            this. Renders line items + total + source URL so the user
            sees a clean breakdown instead of a prose paragraph. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "price_summary")
          .map((a: any, i: number) => (
            <PriceSummaryCard
              key={i}
              title={a.title}
              currency={a.currency || "EUR"}
              lineItems={a.line_items || []}
              totalEur={a.total_eur || 0}
              totalCount={a.total_count || 0}
              sourceUrl={a.source_url}
            />
          ))}
        {/* Venue saved confirmation — save_venue emitted this. Small
            green "✓ saved" card with the new contact's name + a link
            to open it in Contacts. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "venue_saved")
          .map((a: any, i: number) => (
            <VenueSavedCard
              key={i}
              contactId={a.contact_id}
              name={a.name}
              category={a.category}
              url={a.url}
              hasPrices={!!a.has_prices}
            />
          ))}
        {/* yorik_help open-app button — emitted by the help skill so
            users can click through to the relevant screen AFTER reading
            the answer, instead of being auto-navigated mid-read. */}
        {!isUser && message.ui_actions && message.ui_actions
          .filter(a => a.type === "help_open_app")
          .map((a: any, i: number) => (
            <HelpOpenAppCard key={i} app={a.app} path={a.path} />
          ))}
        {/* Debug pane — appears under assistant turns when the user has
            dev_mode ON in Settings. Shows iterations + tool calls +
            args/result snippets + per-step timing. Collapsed by default. */}
        {!isUser && message.agent_trace && (
          <DebugTracePane trace={message.agent_trace} />
        )}
        {/* Feedback thumbs — assistant turns only. Feeds the quality dashboard. */}
        {!isUser && message.content && message.content !== "(no response)" && (
          <TurnFeedback conversationId={conversationId} messageIdx={messageIdx} />
        )}
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// DebugTracePane — collapsible "▼ Debug" under every assistant message
// ---------------------------------------------------------------------------
// Shows: total iterations + total tool calls + wall-clock time,
//        then per-iteration breakdown with each tool call's args + result.
// Hidden behind a <details> caret; default collapsed; no JS state.
// Only renders when the server attached message.agent_trace (which only
// happens when user_profiles.dev_mode = 1).

function DebugTracePane({ trace }: { trace: NonNullable<ChatMessage["agent_trace"]> }) {
  const summary = (() => {
    if (trace.from_cache) {
      return `cache hit • ${trace.total_duration_s.toFixed(2)}s`;
    }
    return `${trace.total_iterations} iter${trace.total_iterations !== 1 ? "s" : ""}` +
           ` • ${trace.total_tool_calls} tool call${trace.total_tool_calls !== 1 ? "s" : ""}` +
           ` • ${trace.total_duration_s.toFixed(2)}s` +
           (trace.halted ? " • halted" : "");
  })();

  const fmtArgs = (args: Record<string, unknown>) => {
    try {
      const s = JSON.stringify(args ?? {}, null, 0);
      return s.length > 240 ? s.slice(0, 240) + "…" : s;
    } catch {
      return "<unserializable>";
    }
  };

  return (
    <details className="mt-2 group/dbg">
      <summary className="cursor-pointer text-[11px] font-mono text-muted-foreground hover:text-foreground select-none flex items-center gap-1.5 py-0.5">
        <span className="group-open/dbg:hidden">▶</span>
        <span className="hidden group-open/dbg:inline">▼</span>
        <span className="opacity-80">Debug</span>
        <span className="opacity-60">({summary})</span>
      </summary>
      <div className="mt-1.5 ml-2 pl-3 border-l border-border/60 font-mono text-[11px] leading-relaxed text-muted-foreground space-y-2 overflow-x-auto">
        {trace.from_cache && trace.note && (
          <div className="italic opacity-80">{trace.note}</div>
        )}
        {trace.iterations.map((it) => (
          <div key={it.n} className="space-y-0.5">
            <div className="text-foreground/80">
              <span className="opacity-60">├</span> Iter {it.n}
              {it.final && <span className="ml-1 opacity-60">— final answer</span>}
              <span className="ml-2 opacity-60">
                llm={it.llm_s.toFixed(2)}s, total={it.duration_s.toFixed(2)}s
                {it.usage?.total_tokens != null && `, ${it.usage.total_tokens} tok`}
              </span>
            </div>
            {it.tool_calls.map((tc, i) => (
              <div key={i} className="ml-4 space-y-0.5">
                <div className="text-foreground/80">
                  <span className="opacity-60">└</span>{" "}
                  {tc.blocked ? <span className="text-amber-600 dark:text-amber-400">⛔ BLOCKED:</span> : "→"}{" "}
                  <span className="text-foreground">{tc.name}</span>
                  <span className="opacity-70">({fmtArgs(tc.args)})</span>
                  <span className="ml-2 opacity-60">{tc.duration_s.toFixed(2)}s</span>
                </div>
                {tc.result && (
                  <div className="ml-5 opacity-75 whitespace-pre-wrap break-words">
                    <span className="opacity-60">←</span>{" "}
                    {tc.result.length > 240 ? tc.result.slice(0, 240) + "…" : tc.result}
                  </div>
                )}
                {tc.ui_actions && tc.ui_actions.length > 0 && (
                  <div className="ml-5 opacity-60">
                    ui_actions: {tc.ui_actions.join(", ")}
                  </div>
                )}
              </div>
            ))}
          </div>
        ))}
      </div>
    </details>
  );
}

// ---------------------------------------------------------------------------
// formatToolStatus — converts a tool name + args dict into a human-friendly
// status line for the typing indicator. Maps the most common Yorik tools
// to a short verb + the thing they're operating on. Falls back to a
// generic "Ruft X auf…" for unknown tools so we never show raw tool
// names in the UI.
// ---------------------------------------------------------------------------
function formatToolStatus(tool: string, args: Record<string, any> | undefined): string {
  const a = args || {};
  // Try a short summary of "what" the tool is operating on.
  const head = (s: string, n = 50) => s.length > n ? s.slice(0, n - 1) + "…" : s;
  switch (tool) {
    case "web_search":   return `🔍 Searching the web for "${head(a.query || "")}"…`;
    case "web_extract":  {
      const urls = Array.isArray(a.urls) ? a.urls : [];
      const host = (() => {
        try { return new URL(urls[0] || "").hostname.replace(/^www\./, ""); }
        catch { return urls[0] || ""; }
      })();
      const more = urls.length > 1 ? ` (+${urls.length - 1})` : "";
      return `📄 Reading ${head(host, 40)}${more}…`;
    }
    case "find_contact":           return `👤 Looking up contact "${head(a.query || "")}"…`;
    case "list_contacts_for_picking": return `👥 Browsing the address book…`;
    case "find_document":          return `📚 Looking for a document about "${head(a.query || "")}"…`;
    case "find_photo":             return `🖼 Looking for a photo of "${head(a.query || "")}"…`;
    case "find_provider_nearby":   return `📍 Looking for ${head(a.poi || "")} near ${head(a.near || "you")}…`;
    case "find_known_provider":    return `🧠 Checking who you already know as a ${head(a.category || "provider")}…`;
    case "calculate_travel_time":  return `🚗 Calculating travel time to ${head(a.to || "")}…`;
    case "compose_check_recipient": return `✉️ Checking the recipient address…`;
    case "compose_check_template_args": return `✉️ Checking the template fields…`;
    case "compose_draft":          return `✉️ Drafting…`;
    case "find_recipient_address_from_documents": return `🗂 Looking for the address in old documents…`;
    case "propose_inline_photo":   return `🖼 Looking for photo suggestions…`;
    case "add_calendar_event":     return `📅 Adding an event…`;
    case "update_calendar_event":  return `📅 Updating an event…`;
    case "delete_calendar_event":  return `📅 Removing an event…`;
    case "check_calendar":         return `📅 Checking your calendar…`;
    case "add_task":               return `✅ Adding a task…`;
    case "check_tasks":            return `✅ Checking your tasks…`;
    case "add_bill":               return `💰 Adding a bill…`;
    case "check_bills":            return `💰 Checking your bills…`;
    case "run_sql":                return `🗃 Querying the database…`;
    case "search_documents":       return `📚 Searching your documents…`;
    case "trigger_connector":      return `🔌 Calling ${head(a.name || "connector")}…`;
    case "navigate_to":            return `🧭 Opening ${head(a.app || "app")}…`;
    case "yorik_help":              return `📖 Checking the docs${a.topic ? ` (${head(a.topic)})` : ""}…`;
    default:                       return `⚙ Calling ${tool}…`;
  }
}

// ---------------------------------------------------------------------------
// Web-search results card — Yorik called web_lookup
// ---------------------------------------------------------------------------
// Renders 3-5 result cards (title + URL + snippet). Each "Details" button
// fires a follow-up chat message asking the LLM to web_fetch that URL
// with a sensible question. Designed to feel like search snippets, with
// a "via DuckDuckGo / Brave / SearXNG" footer for source transparency.

function WebResultsCard({
  query, provider, results,
}: {
  query: string;
  provider: string;
  results: Array<{ title: string; url: string; snippet: string }>;
}) {
  if (!results || results.length === 0) return null;

  function shortHost(url: string): string {
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch { return url.slice(0, 30); }
  }

  function ask(url: string, title: string) {
    // Reuses the existing seed-and-send event the chat already listens
    // on (line ~408). Lands as a user message + auto-sends, so the
    // LLM's next turn calls web_fetch on the chosen URL.
    const evt = new CustomEvent("yorik:chat-seed-and-send", {
      detail: {
        seed: `Please read "${title.slice(0, 60)}" and summarize the key points for me. URL: ${url}`,
      },
    });
    window.dispatchEvent(evt);
  }

  return (
    <div className="mt-3 rounded-2xl border border-blue-500/30 bg-blue-500/[0.04] overflow-hidden">
      <div className="px-4 py-2.5 border-b border-blue-500/20 flex items-center gap-2 text-xs">
        <Globe className="w-3.5 h-3.5 text-blue-500" />
        <span className="font-medium">Web search:</span>
        <span className="text-muted-foreground truncate">{query}</span>
        <span className="ml-auto text-[10px] text-muted-foreground uppercase tracking-wider">
          via {provider}
        </span>
      </div>
      <ul className="divide-y divide-blue-500/15">
        {results.map((r, i) => (
          <li key={i} className="px-4 py-2.5">
            <a
              href={r.url}
              target="_blank"
              rel="noopener nofollow"
              className="text-sm font-medium text-blue-600 dark:text-blue-300 hover:underline truncate block"
            >
              {r.title}
            </a>
            <div className="text-[10px] text-muted-foreground truncate">
              {shortHost(r.url)}
            </div>
            {r.snippet && (
              <div className="text-xs text-foreground/80 mt-1 line-clamp-2 leading-snug">
                {r.snippet}
              </div>
            )}
            <button
              onClick={() => ask(r.url, r.title)}
              className="mt-1.5 text-[11px] px-2 py-0.5 rounded bg-blue-500/15 hover:bg-blue-500/25 text-blue-700 dark:text-blue-300 transition"
            >
              Details holen
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Venue-saved card — save_venue emitted this. Small green confirmation
// with a quick link to open the new contact in the Contacts app.
// ---------------------------------------------------------------------------
function VenueSavedCard({
  contactId, name, category, url, hasPrices,
}: {
  contactId: number;
  name: string;
  category?: string;
  url?: string;
  hasPrices?: boolean;
}) {
  return (
    <div className="mt-3 rounded-2xl border border-emerald-500/30 bg-emerald-500/[0.05] px-4 py-2.5 flex items-center gap-3 max-w-md">
      <div className="w-7 h-7 rounded-full bg-emerald-500/15 flex items-center justify-center shrink-0">
        <Check className="w-4 h-4 text-emerald-600" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium truncate">{name} gespeichert</div>
        <div className="text-[11px] text-muted-foreground truncate">
          {category && <>as "{category}" · </>}
          {hasPrices ? "Prices saved · " : ""}
          {url ? (
            <a href={url} target="_blank" rel="noopener nofollow"
                className="text-blue-600 dark:text-blue-300 hover:underline">
              Website
            </a>
          ) : "Available instantly next time"}
        </div>
      </div>
      <a
        href={`/r/contacts?id=${contactId}`}
        className="text-[11px] text-emerald-700 dark:text-emerald-300 underline hover:no-underline shrink-0"
      >
        Open
      </a>
    </div>
  );
}

// ---------------------------------------------------------------------------
// HelpOpenAppCard — click-through button under a yorik_help answer
// ---------------------------------------------------------------------------
// The yorik_help skill returns the help-doc body for the LLM to quote.
// Instead of auto-navigating (which yanks the user away mid-read), the
// skill emits this card so the user clicks when they're ready.

const HELP_APP_LABELS: Record<string, string> = {
  home: "Home", calendar: "Calendar", chat: "Chat",
  contacts: "Contacts", documents: "Documents", compose: "Compose",
  email: "Email", photos: "Photos", tasks: "Tasks",
  whatsapp: "WhatsApp", briefing: "Daily briefing", settings: "Settings",
};

function HelpOpenAppCard({ app, path }: { app: string; path: string }) {
  const key = (app || "").toLowerCase();
  const label = HELP_APP_LABELS[key] || (key ? key[0].toUpperCase() + key.slice(1) : "App");
  return (
    <a
      href={path}
      className="mt-3 inline-flex items-center gap-2 rounded-full border border-blue-500/30 bg-blue-500/[0.06] px-3.5 py-1.5 text-sm font-medium text-blue-700 dark:text-blue-300 hover:bg-blue-500/[0.12] transition-colors max-w-fit"
    >
      <span>Open {label}</span>
      <span aria-hidden="true">→</span>
    </a>
  );
}

// ---------------------------------------------------------------------------
// Price-summary card — receipt-style total for a group of people
// ---------------------------------------------------------------------------
// compute_group_price emits this. Renders line items + total + source URL
// as a small invoice-like block so the user can scan the breakdown without
// re-reading prose.

function PriceSummaryCard({
  title, currency, lineItems, totalEur, totalCount, sourceUrl,
}: {
  title?: string;
  currency: string;
  lineItems: Array<{ label: string; unit_eur: number; count: number; subtotal_eur: number }>;
  totalEur: number;
  totalCount: number;
  sourceUrl?: string;
}) {
  if (!lineItems || lineItems.length === 0) return null;
  const symbol = currency === "EUR" ? "€" : currency;
  function fmt(v: number) { return v.toFixed(2).replace(".", ","); }
  function shortHost(url: string): string {
    try { return new URL(url).hostname.replace(/^www\./, ""); }
    catch { return url.slice(0, 30); }
  }

  return (
    <div className="mt-3 rounded-2xl border border-emerald-500/30 bg-emerald-500/[0.04] overflow-hidden max-w-md">
      {title && (
        <div className="px-4 py-2.5 border-b border-emerald-500/20 flex items-center gap-2 text-xs">
          <span className="font-medium">{title}</span>
          <span className="ml-auto text-[10px] text-muted-foreground tabular-nums">
            {totalCount} {totalCount === 1 ? "Person" : "Personen"}
          </span>
        </div>
      )}
      <ul className="divide-y divide-emerald-500/15">
        {lineItems.map((li, i) => (
          <li key={i} className="px-4 py-2 flex items-center gap-3 text-sm">
            <span className="text-foreground/85 truncate flex-1 min-w-0">
              {li.count}× {li.label}
            </span>
            <span className="text-[11px] text-muted-foreground tabular-nums shrink-0">
              {fmt(li.unit_eur)} {symbol}
            </span>
            <span className="font-medium tabular-nums shrink-0 w-20 text-right">
              {fmt(li.subtotal_eur)} {symbol}
            </span>
          </li>
        ))}
      </ul>
      <div className="px-4 py-2.5 border-t border-emerald-500/30 bg-emerald-500/10 flex items-center justify-between text-sm font-semibold">
        <span>Gesamt</span>
        <span className="tabular-nums">{fmt(totalEur)} {symbol}</span>
      </div>
      {sourceUrl && (
        <div className="px-4 py-1.5 text-[10px] text-muted-foreground border-t border-emerald-500/15 truncate">
          Preise laut{" "}
          <a href={sourceUrl} target="_blank" rel="noopener nofollow"
              className="text-blue-600 dark:text-blue-300 hover:underline">
            {shortHost(sourceUrl)}
          </a>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Compose-draft card — chat→compose handoff
// ---------------------------------------------------------------------------
// Rendered below an assistant message when the LLM called compose_draft.
// Click "Bearbeiten →" opens /r/compose?draft_id=N with the body, recipient,
// and subject pre-filled in the editor.

function ComposeDraftCard({
  draftId, kind, recipient, subject, preview,
  templateId, templateName, alternates, missingArgs,
}: {
  draftId: number;
  kind: string;
  recipient?: string;
  subject?: string;
  preview?: string;
  templateId?: string | null;
  templateName?: string | null;
  alternates?: Array<{ id: string; name: string; score?: number }>;
  missingArgs?: string[];
}) {
  const navigate = useNavigate();
  const [showAlts, setShowAlts] = useState(false);
  const kindLabel: Record<string, string> = {
    letter:  "Brief",
    invoice: "Rechnung",
    offer:   "Angebot",
    email:   "E-Mail",
    memo:    "Notiz",
  };
  const kindIcon = kind === "invoice" || kind === "offer" ? "💶" : kind === "email" ? "📧" : "📄";
  const hasAlts = (alternates?.length ?? 0) > 0;
  return (
    <div className="mt-2 border border-border rounded-xl bg-card/80 overflow-hidden max-w-md">
      <div className="px-3 pt-2.5 pb-2">
        <div className="flex items-center gap-1.5 mb-1.5">
          <span className="text-base leading-none">{kindIcon}</span>
          <span className="text-xs font-semibold">{kindLabel[kind] || "Dokument"} vorbereitet</span>
          <span className="text-[9px] text-muted-foreground font-mono ml-auto">#{draftId}</span>
        </div>
        <div className="space-y-0.5 text-xs">
          {recipient && (
            <div><span className="text-muted-foreground">An:</span> <span className="font-medium">{recipient}</span></div>
          )}
          {subject && (
            <div><span className="text-muted-foreground">Betreff:</span> <span className="font-medium">{subject}</span></div>
          )}
          {preview && (
            <div className="text-muted-foreground italic mt-1 line-clamp-2">{preview}…</div>
          )}
          {(missingArgs?.length ?? 0) > 0 && (
            <div className="mt-1.5 text-[11px] text-amber-600 dark:text-amber-400">
              Still empty: {missingArgs!.join(", ")} — open in Compose to fill in
            </div>
          )}
        </div>
        {(templateName || hasAlts) && (
          <div className="mt-2 pt-2 border-t border-border/50 text-[11px]">
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <span>Template:</span>
              <span className="font-medium text-foreground">{templateName || "none"}</span>
              {hasAlts && (
                <button
                  onClick={() => setShowAlts(s => !s)}
                  className="ml-auto text-violet-500 hover:underline"
                >
                  {showAlts ? "close" : "switch"}
                </button>
              )}
            </div>
            {showAlts && hasAlts && (
              <div className="mt-1.5 space-y-1">
                {alternates!.map(alt => (
                  <button
                    key={alt.id}
                    onClick={() => {
                      // Open Compose with the draft id + template hint so
                      // the editor can swap rendering immediately.
                      navigate(`/compose?draft_id=${draftId}&template=${encodeURIComponent(alt.id)}`);
                    }}
                    className="w-full text-left px-2 py-1 rounded hover:bg-muted/60 flex items-center gap-2"
                  >
                    <span className="text-foreground/80">↻</span>
                    <span className="font-medium">{alt.name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
      <div className="px-3 pb-2.5 pt-1.5 border-t border-border bg-muted/20 flex items-center justify-end gap-1.5">
        <button
          onClick={() => navigate(`/compose?draft_id=${draftId}`)}
          className="text-[11px] px-3 py-1.5 rounded-md bg-violet-500 hover:bg-violet-600 text-white font-medium transition inline-flex items-center gap-1 shadow-sm"
        >
          Bearbeiten <span aria-hidden>→</span>
        </button>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Template picker — chat asks "which template?" so the user can choose
// ---------------------------------------------------------------------------
//
// Rendered when pick_compose_template emits `template_picker` with the
// candidate template_ids the LLM chose. Each option triggers a follow-up
// chat message carrying the picked id, so the LLM's next compose_draft
// call includes it explicitly.

type TemplateCandidate = {
  id:             string;
  name:           string;
  description?:   string;
  kind?:          string;          // letter / email / invoice / offer / memo
  when_to_use?:   string[];        // bullets, used for the subline
};

function TemplatePickerCard({
  query, templates,
}: {
  query: string;
  templates: TemplateCandidate[];
}) {
  // Click → seed `[template_picked id=X]`. The explicit prefix makes
  // it unambiguous for the LLM that the next step is compose_draft
  // with the chosen template, not "let me re-rank again".
  const pick = (t: TemplateCandidate) => {
    const seed = `[template_picked id=${t.id}] Use this template (${t.name}) for the rest of the request. Proceed straight to compose_check_recipient → compose_draft with the original intent.`;
    try { sessionStorage.setItem("yorik_chat_seed", seed); } catch {}
    window.dispatchEvent(new CustomEvent("yorik:chat-seed-and-send", { detail: { seed } }));
  };

  const findMore = () => {
    const seed = `Andere Vorlage suchen — call list_compose_templates again, read the full list, and call pick_compose_template with 3 broader candidates than before.`;
    try { sessionStorage.setItem("yorik_chat_seed", seed); } catch {}
    window.dispatchEvent(new CustomEvent("yorik:chat-seed-and-send", { detail: { seed } }));
  };

  const kindIcon = (kind?: string): string => {
    if (kind === "invoice" || kind === "offer") return "💶";
    if (kind === "email") return "📧";
    if (kind === "memo")  return "📝";
    return "📄";
  };

  if (!templates.length) {
    return (
      <div className="mt-2 px-3 py-2 rounded-xl bg-amber-500/[0.06] border border-amber-500/30 max-w-md text-xs text-foreground">
        No templates available. Install some under Settings → Compose.
      </div>
    );
  }

  return (
    <div className="mt-2 border border-blue-500/30 rounded-xl bg-blue-500/[0.04] overflow-hidden max-w-md">
      <div className="px-3 pt-2.5 pb-1.5 text-[11px] text-blue-500 font-semibold uppercase tracking-wider">
        Welche Vorlage? {query ? `(„${query}")` : ""}
      </div>
      <div className="divide-y divide-blue-500/10">
        {templates.slice(0, 3).map(t => {
          // Subline preference: top 2 when_to_use bullets (richer) →
          // fall back to description (back-compat with old emitter).
          const subline = (t.when_to_use && t.when_to_use.length > 0)
            ? t.when_to_use.slice(0, 2).join(" · ")
            : (t.description || "");
          return (
            <button
              key={t.id}
              onClick={() => pick(t)}
              className="w-full text-left px-3 py-2 hover:bg-blue-500/[0.06] transition group"
            >
              <div className="flex items-center gap-2">
                <span className="text-base leading-none">{kindIcon(t.kind)}</span>
                <span className="text-sm font-medium flex-1 truncate">{t.name}</span>
                <span className="text-[10px] text-blue-500 opacity-0 group-hover:opacity-100 transition">
                  pick →
                </span>
              </div>
              {subline && (
                <div className="text-[11px] text-muted-foreground mt-0.5 line-clamp-2">
                  {subline}
                </div>
              )}
            </button>
          );
        })}
      </div>
      <button
        onClick={findMore}
        className="w-full text-center px-3 py-1.5 text-[11px] text-muted-foreground hover:text-foreground hover:bg-blue-500/[0.06] transition border-t border-blue-500/10"
      >
        Andere Vorlage suchen…
      </button>
    </div>
  );
}


// ---------------------------------------------------------------------------
// POI picker — find_provider_nearby returned a list, render searchable card
// ---------------------------------------------------------------------------
//
// The skill emits `pois_found` with the full list (default limit 12). The
// chat prose only quotes the top 3-5 so the user doesn't drown in text;
// this card shows ALL of them with a search box and a "none of these"
// escape hatch (covers the case where the user wants to add their own
// provider by name instead of picking from nearby search results).

type Poi = {
  name: string;
  address?: string;
  phone?: string;
  website?: string;
  lat?: number;
  lon?: number;
};

function PoiPickerCard({
  poi: poiCategory,
  near,
  pois,
}: {
  poi: string;
  near: string;
  pois: Poi[];
}) {
  const [query, setQuery] = useState("");

  if (!pois.length) return null;

  const filtered = pois.filter(p => {
    if (!query.trim()) return true;
    const needle = query.toLowerCase();
    return (
      (p.name || "").toLowerCase().includes(needle) ||
      (p.address || "").toLowerCase().includes(needle)
    );
  });

  const pick = (p: Poi) => {
    const parts = [p.name];
    if (p.address) parts.push(p.address);
    const seed = `Ich nehme: ${parts.join(", ")}`;
    try { sessionStorage.setItem("yorik_chat_seed", seed); } catch {}
    window.dispatchEvent(new CustomEvent("yorik:chat-seed-and-send", { detail: { seed } }));
  };

  const pickOwn = () => {
    const seed = `Keiner davon — ich habe meinen eigenen ${poiCategory}. Frag mich nach dem Namen.`;
    try { sessionStorage.setItem("yorik_chat_seed", seed); } catch {}
    window.dispatchEvent(new CustomEvent("yorik:chat-seed-and-send", { detail: { seed } }));
  };

  return (
    <div className="mt-2 border border-violet-500/30 rounded-xl bg-violet-500/[0.04] overflow-hidden max-w-md">
      <div className="px-3 pt-2.5 pb-1.5 text-[11px] text-violet-500 font-semibold uppercase tracking-wider">
        {poiCategory}{near ? ` near ${near}` : ""} — please pick one
      </div>
      {pois.length > 5 && (
        <div className="px-3 pb-2">
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search (name or address)…"
            className="w-full px-2 py-1 text-sm bg-background border border-border rounded-md focus:outline-none focus:border-violet-500/50"
          />
        </div>
      )}
      <div className="divide-y divide-violet-500/10 max-h-64 overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="px-3 py-4 text-xs text-muted-foreground text-center">
            No matches for "{query}"
          </div>
        ) : filtered.map((p, i) => (
          <button
            key={i}
            onClick={() => pick(p)}
            className="w-full text-left px-3 py-2 hover:bg-violet-500/[0.06] transition group"
          >
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium flex-1 truncate">{p.name}</span>
              <span className="text-[10px] text-violet-500 opacity-0 group-hover:opacity-100 transition">
                pick →
              </span>
            </div>
            {p.address && (
              <div className="text-[11px] text-muted-foreground mt-0.5 line-clamp-1">
                {p.address}
              </div>
            )}
            {p.phone && (
              <div className="text-[11px] text-muted-foreground/80 mt-0.5 line-clamp-1">
                {p.phone}
              </div>
            )}
          </button>
        ))}
      </div>
      <button
        onClick={pickOwn}
        className="w-full text-left px-3 py-2 border-t border-violet-500/10 text-[11px] text-violet-500 hover:bg-violet-500/[0.06] transition font-medium"
      >
        Keiner davon — eigenen Kontakt eintragen
      </button>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Contact picker — find_contact returned >1 candidate, render click-to-pick
// ---------------------------------------------------------------------------
//
// User sees a card with each candidate's name + distinguisher (relation,
// email, phone). Click → seeds a follow-up message naming the contact_id
// so the LLM disambiguates without the user having to type the name back.

type ContactCandidate = {
  id: number;
  display_name: string;
  relation?: string;
  kind?: string;
  email?: string | null;
  phone?: string | null;
  whatsapp?: string | null;
  address?: string | null;
  /** 0..1 — present when the LLM ranked these via the render-mode call
   *  of list_contacts_for_picking (ranked_picks=[...]). Rendered as a
   *  small percentage pill on the row. */
  confidence?: number;
  /** One-liner explaining WHY this pick matches the user's phrase
   *  ("relation='Großmutter'", "alias 'BKK' matches business name").
   *  Rendered as a muted third row when present. */
  reason?: string;
};

function ContactPickerCard({
  query,
  contacts,
  ranked = false,
}: {
  query: string;
  contacts: ContactCandidate[];
  /** When true, the contacts came from list_contacts_for_picking's
   *  render mode (ranked_picks=[...]) with confidence + reason fields.
   *  Header copy + row ordering shift to match. */
  ranked?: boolean;
}) {
  const [browseOpen, setBrowseOpen] = useState(false);

  if (!contacts.length) return null;

  const pick = (c: ContactCandidate) => {
    // Include the id so the LLM doesn't have to re-resolve. Use the
    // same chat-seed pattern as the other pickers.
    const tag = c.relation ? ` (${c.relation})` : "";
    const seed = `Ich meine: ${c.display_name}${tag}, contact_id=${c.id}`;
    try { sessionStorage.setItem("yorik_chat_seed", seed); } catch {}
    window.dispatchEvent(new CustomEvent("yorik:chat-seed-and-send", { detail: { seed } }));
  };

  // When ranked, sort by confidence DESC defensively. Server already
  // sorts but the LLM may have included items with equal/missing confidence.
  const ordered = ranked
    ? [...contacts].sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0))
    : contacts;

  return (
    <>
      <div className="mt-2 border border-violet-500/30 rounded-xl bg-violet-500/[0.04] overflow-hidden max-w-md">
        <div className="px-3 pt-2.5 pb-1.5 text-[11px] text-violet-500 font-semibold uppercase tracking-wider">
          {ranked ? "Meinst du" : "Welcher Kontakt?"} {query ? `(„${query}")` : ""}
        </div>
        <div className="divide-y divide-violet-500/10 max-h-72 overflow-y-auto">
          {ordered.map((c, i) => {
            // Lead with relation/kind, then address on its own line, then
            // a compact contact-channel row (phone · email · whatsapp).
            const header = c.relation
              || (c.kind === "business" ? "Unternehmen" : "Person");
            const channels = [
              c.phone || "",
              c.email || "",
              c.whatsapp ? c.whatsapp.replace(/@.*$/, "") : "",
            ].filter(Boolean).join(" · ");
            const conf = typeof c.confidence === "number" ? Math.round(c.confidence * 100) : null;
            const confTone =
              conf === null      ? "" :
              conf >= 80         ? "bg-emerald-500/15 text-emerald-500" :
              conf >= 50         ? "bg-amber-500/15 text-amber-500" :
                                   "bg-muted text-muted-foreground";
            return (
              <button
                key={c.id}
                onClick={() => pick(c)}
                className="w-full text-left px-3 py-2 hover:bg-violet-500/[0.06] transition group"
              >
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted-foreground tabular-nums shrink-0 w-6 text-right">{i + 1}.</span>
                  <span className="text-sm font-medium flex-1 truncate">{c.display_name}</span>
                  {conf !== null && (
                    <span
                      className={cn(
                        "text-[10px] px-1.5 py-0.5 rounded-full font-medium tabular-nums shrink-0",
                        confTone,
                      )}
                      title={c.reason || "LLM-Konfidenz"}
                    >
                      {conf}%
                    </span>
                  )}
                  <span className="text-[10px] text-violet-500 opacity-0 group-hover:opacity-100 transition">
                    pick →
                  </span>
                </div>
                <div className="text-[11px] text-muted-foreground mt-0.5 line-clamp-1">
                  {header}
                </div>
                {c.reason && (
                  <div className="text-[11px] text-violet-500/80 mt-0.5 line-clamp-2 italic">
                    {c.reason}
                  </div>
                )}
                {c.address && (
                  <div className="text-[11px] text-muted-foreground/80 mt-0.5 line-clamp-1">
                    {c.address}
                  </div>
                )}
                {channels && (
                  <div className="text-[11px] text-muted-foreground/80 mt-0.5 line-clamp-1">
                    {channels}
                  </div>
                )}
              </button>
            );
          })}
        </div>
        <button
          type="button"
          onClick={() => setBrowseOpen(true)}
          className="w-full px-3 py-2 text-[11px] text-violet-500 hover:bg-violet-500/[0.06] border-t border-violet-500/10 text-left flex items-center gap-1.5"
        >
          <Search className="w-3 h-3" />
          Keiner davon — aus Kontaktliste wählen →
        </button>
      </div>
      {browseOpen && (
        <AllContactsBrowserModal
          onClose={() => setBrowseOpen(false)}
          onPick={(c) => { pick(c); setBrowseOpen(false); }}
        />
      )}
    </>
  );
}


// ---------------------------------------------------------------------------
// AllContactsBrowserModal — fallback when none of the suggested candidates
// match. Searchable list of all active contacts, click → seeds the same
// "Ich meine: …" follow-up as ContactPickerCard so the LLM resumes the
// flow with the chosen contact_id.
// ---------------------------------------------------------------------------

function AllContactsBrowserModal({
  onClose,
  onPick,
}: {
  onClose: () => void;
  onPick: (c: ContactCandidate) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ContactCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Debounced search against the same endpoint RecipientPicker uses.
  useEffect(() => {
    const handle = window.setTimeout(async () => {
      setLoading(true);
      try {
        const params = new URLSearchParams({ status: "active", limit: "500" });
        if (query.trim()) params.set("q", query.trim());
        const list = await api.get<ContactCandidate[]>(`/api/contacts?${params}`);
        setResults(Array.isArray(list) ? list : []);
      } catch (e) {
        console.error("contact-browser: search failed", e);
        setResults([]);
      } finally {
        setLoading(false);
      }
    }, 200);
    return () => window.clearTimeout(handle);
  }, [query]);

  useEffect(() => {
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[1000] bg-black/60 backdrop-blur-sm flex items-center justify-center p-6"
      onClick={onClose}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md max-h-[80vh] flex flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="flex items-center gap-2">
            <UsersRound className="w-4 h-4 text-violet-500" />
            <span className="text-sm font-semibold">Kontakt wählen</span>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground transition"
            title="Schließen"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="px-4 py-3 border-b border-border">
          <div className="relative">
            <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              ref={inputRef}
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Name, Beziehung oder Alias…"
              className="w-full h-9 pl-8 pr-8 bg-muted/60 rounded-md text-sm focus:outline-none focus:ring-1 focus:ring-violet-500/40"
            />
            {query && (
              <button
                onClick={() => setQuery("")}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 p-0.5 text-muted-foreground hover:text-foreground"
                title="Suche leeren"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="flex items-center justify-center py-6 text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin" />
            </div>
          )}
          {!loading && results.length === 0 && (
            <div className="text-center py-6 text-xs text-muted-foreground italic">
              {query ? `Keine Treffer für „${query}"` : "Keine Kontakte"}
            </div>
          )}
          {!loading && results.map((c, i) => {
            const header = c.relation
              || (c.kind === "business" ? "Unternehmen" : "Person");
            return (
              <button
                key={c.id}
                onClick={() => onPick(c)}
                className="w-full text-left px-4 py-2.5 hover:bg-violet-500/[0.06] transition border-b border-violet-500/10 last:border-b-0"
              >
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-muted-foreground tabular-nums shrink-0 w-6 text-right">{i + 1}.</span>
                  <span className="text-sm font-medium flex-1 truncate">{c.display_name}</span>
                  <span className="text-[10px] text-violet-500 shrink-0">pick →</span>
                </div>
                <div className="text-[11px] text-muted-foreground mt-0.5 line-clamp-1 pl-8">
                  {header}
                </div>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// TasksFoundCard — clickable task rows inside a chat bubble.
//   - Checkbox marks the task done via PATCH (optimistic) so the user can
//     burn through their list without leaving chat.
//   - Click on the row → /tasks?task=ID with the highlight pulse.
//   - "Alle in Aufgaben öffnen →" footer link when total > shown count.
// ---------------------------------------------------------------------------

type TaskCardRow = {
  id: number;
  title: string;
  due_date: string | null;
  done: number | boolean;
  person?: string | null;
  category?: string | null;
  priority?: number | null;
  recurrence_rule?: string | null;
  subtasks?: { open: number; done: number } | null;
};

function TasksFoundCard({
  tasks,
  total,
}: {
  tasks: TaskCardRow[];
  total: number;
}) {
  const navigate = useNavigate();
  const [localDone, setLocalDone] = useState<Record<number, boolean>>({});

  if (!tasks.length) return null;

  const todayIso = new Date().toISOString().slice(0, 10);
  const isOverdue = (d: string | null) => !!d && d < todayIso;
  const isToday   = (d: string | null) => d === todayIso;

  function fmtDue(d: string | null): string {
    if (!d) return "no date";
    if (d === todayIso) return "today";
    // Render YYYY-MM-DD as MMM D for a compact English date
    const dt = new Date(d + "T00:00:00");
    if (Number.isNaN(dt.getTime())) return d;
    return dt.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  async function toggleDone(id: number, current: boolean) {
    // Optimistic flip first — the user shouldn't wait for the round-trip.
    setLocalDone(prev => ({ ...prev, [id]: !current }));
    try {
      await api.patch(`/api/tasks/${id}`, { done: !current });
    } catch (e) {
      // Roll back on failure. No toast in chat — log and let the user retry.
      setLocalDone(prev => ({ ...prev, [id]: current }));
      console.error("[tasks_found] toggle failed", e);
    }
  }

  return (
    <div className="mt-2 border border-emerald-500/30 rounded-xl bg-emerald-500/[0.04] overflow-hidden max-w-md">
      <div className="px-3 pt-2.5 pb-1.5 text-[11px] text-emerald-500 font-semibold uppercase tracking-wider flex items-center gap-1.5">
        <CheckSquare className="w-3 h-3" />
        Aufgaben ({total})
      </div>
      <div className="divide-y divide-emerald-500/10 max-h-[420px] overflow-y-auto">
        {tasks.map(t => {
          const doneNow = localDone[t.id] ?? !!t.done;
          const overdue = !doneNow && isOverdue(t.due_date);
          const today   = !doneNow && isToday(t.due_date);
          return (
            <div
              key={t.id}
              className="flex items-start gap-2.5 px-3 py-2 hover:bg-emerald-500/[0.06] transition group"
            >
              <button
                type="button"
                onClick={() => toggleDone(t.id, doneNow)}
                className={cn(
                  "mt-0.5 h-4 w-4 rounded border flex items-center justify-center transition shrink-0",
                  doneNow
                    ? "bg-emerald-500 border-emerald-500 text-white"
                    : "border-muted-foreground/40 hover:border-emerald-500"
                )}
                title={doneNow ? "Als offen markieren" : "Als erledigt markieren"}
              >
                {doneNow && <Check className="w-3 h-3" />}
              </button>
              <button
                type="button"
                onClick={() => navigate(`/tasks?task=${t.id}`)}
                className="flex-1 min-w-0 text-left"
              >
                <div className={cn(
                  "text-sm font-medium truncate",
                  doneNow && "line-through text-muted-foreground"
                )}>
                  {t.title}
                </div>
                <div className="text-[11px] text-muted-foreground mt-0.5 flex items-center gap-1.5 flex-wrap">
                  <span className={cn(
                    overdue && "text-red-500 font-medium",
                    today && "text-amber-500 font-medium",
                  )}>
                    {fmtDue(t.due_date)}
                  </span>
                  {t.person && <span>· {t.person}</span>}
                  {t.category && <span>· {t.category}</span>}
                  {(t.priority || 0) >= 2 && <span>· ⚑</span>}
                  {t.recurrence_rule && <span title={t.recurrence_rule}>· ↻</span>}
                  {t.subtasks && (t.subtasks.open + t.subtasks.done) > 0 && (
                    <span>· {t.subtasks.done}/{t.subtasks.open + t.subtasks.done}</span>
                  )}
                </div>
              </button>
            </div>
          );
        })}
      </div>
      {total > tasks.length && (
        <button
          type="button"
          onClick={() => navigate("/tasks")}
          className="w-full px-3 py-2 text-[11px] text-emerald-500 hover:bg-emerald-500/[0.06] border-t border-emerald-500/10 text-left"
        >
          Open all {total} in /tasks →
        </button>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// EventsFoundCard — clickable event rows in a chat bubble.
//   - Click on a row → /calendar?event=ID (deep-link selects it).
//   - Shows title + time/date; collapses date for same-day events.
//   - "Open all in /calendar →" footer link when total > shown count.
// ---------------------------------------------------------------------------

type EventCardRow = {
  id: number;
  title: string;
  starts_at: string;
  ends_at: string | null;
  all_day?: boolean;
  person?: string | null;
  date?: string;
  weekday?: string;
  time?: string;
  who?: string;
};

function EventsFoundCard({
  events,
  total,
}: {
  events: EventCardRow[];
  total: number;
}) {
  const navigate = useNavigate();
  if (!events.length) return null;

  const todayIso = new Date().toISOString().slice(0, 10);
  // When every event is on the same date, drop the redundant date column.
  const allOnOneDate =
    events.length > 1 &&
    events.every(e => (e.date || e.starts_at.slice(0, 10)) === (events[0].date || events[0].starts_at.slice(0, 10)));

  function fmtDate(iso: string): string {
    if (iso === todayIso) return "today";
    const dt = new Date(iso + "T00:00:00");
    if (Number.isNaN(dt.getTime())) return iso;
    return dt.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  }

  function fmtTime(starts: string, ends: string | null, allDay?: boolean): string {
    if (allDay) return "all day";
    const startTime = starts.slice(11, 16);
    const endTime = ends ? ends.slice(11, 16) : "";
    return endTime ? `${startTime}–${endTime}` : startTime;
  }

  return (
    <div className="mt-2 border border-sky-500/30 rounded-xl bg-sky-500/[0.04] overflow-hidden max-w-md">
      <div className="px-3 pt-2.5 pb-1.5 text-[11px] text-sky-500 font-semibold uppercase tracking-wider flex items-center gap-1.5">
        <Calendar className="w-3 h-3" />
        Termine ({total})
        {allOnOneDate && (
          <span className="font-normal opacity-70 normal-case tracking-normal">
            · {fmtDate(events[0].date || events[0].starts_at.slice(0, 10))}
          </span>
        )}
      </div>
      <div className="divide-y divide-sky-500/10 max-h-[420px] overflow-y-auto">
        {events.map(e => {
          const dateIso = e.date || e.starts_at.slice(0, 10);
          const who = e.who || e.person || "";
          return (
            <button
              key={e.id}
              type="button"
              onClick={() => navigate(`/calendar?event=${e.id}`)}
              className="w-full text-left px-3 py-2 hover:bg-sky-500/[0.06] transition group"
            >
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium flex-1 truncate">{e.title}</span>
                <span className="text-[10px] text-sky-500 opacity-0 group-hover:opacity-100 transition shrink-0">
                  open →
                </span>
              </div>
              <div className="text-[11px] text-muted-foreground mt-0.5 flex items-center gap-1.5 flex-wrap tabular-nums">
                <span>{fmtTime(e.starts_at, e.ends_at, e.all_day)}</span>
                {!allOnOneDate && <span>· {fmtDate(dateIso)}</span>}
                {who && <span>· {who}</span>}
              </div>
            </button>
          );
        })}
      </div>
      {total > events.length && (
        <button
          type="button"
          onClick={() => navigate("/calendar")}
          className="w-full px-3 py-2 text-[11px] text-sky-500 hover:bg-sky-500/[0.06] border-t border-sky-500/10 text-left"
        >
          Open all {total} in /calendar →
        </button>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// ContactsFoundCard — searchable, clickable contact rows in a chat bubble.
//   - Local filter input narrows the list as the user types.
//   - Click → /contacts?contact=ID (deep-link selects it).
//   - "Alle in /kontakte öffnen →" footer link when total > shown count.
// ---------------------------------------------------------------------------

type ContactCardRow = {
  id: number;
  display_name: string;
  relation: string | null;
  kind: string | null;
};

function ContactsFoundCard({
  contacts,
  total,
  filter,
  pickToChat = false,
}: {
  contacts: ContactCardRow[];
  total: number;
  filter: string | null;
  /** When true, clicking a row seeds a follow-up chat message ("I meant
   *  this one, contact_id=N") so the LLM resumes the draft/picker flow
   *  with the chosen contact. False (default) keeps the original
   *  navigate-to-contact behavior for browse-style intents. The skill
   *  sets this via the pick_to_chat input. */
  pickToChat?: boolean;
}) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");

  if (!contacts.length) return null;

  const q = query.trim().toLowerCase();
  const shown = q
    ? contacts.filter(c =>
        c.display_name.toLowerCase().includes(q)
        || (c.relation || "").toLowerCase().includes(q)
      )
    : contacts;

  const handleClick = (c: ContactCardRow) => {
    if (pickToChat) {
      const tag = c.relation ? ` (${c.relation})` : "";
      const seed = `Ich meine: ${c.display_name}${tag}, contact_id=${c.id}`;
      try { sessionStorage.setItem("yorik_chat_seed", seed); } catch {}
      window.dispatchEvent(new CustomEvent("yorik:chat-seed-and-send", { detail: { seed } }));
      return;
    }
    navigate(`/contacts?contact=${c.id}`);
  };

  return (
    <div className="mt-2 border border-violet-500/30 rounded-xl bg-violet-500/[0.04] overflow-hidden max-w-md">
      <div className="px-3 pt-2.5 pb-1.5 text-[11px] text-violet-500 font-semibold uppercase tracking-wider flex items-center gap-1.5">
        <UsersRound className="w-3 h-3" />
        Kontakte ({total}{filter ? ` · ${filter}` : ""})
      </div>
      {contacts.length > 5 && (
        <div className="px-3 pb-2">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter (Name oder Beziehung)…"
            className="w-full text-xs px-2 py-1.5 rounded-md bg-background border border-violet-500/20 focus:border-violet-500 focus:outline-none"
          />
        </div>
      )}
      <div className="divide-y divide-violet-500/10 max-h-[420px] overflow-y-auto">
        {shown.map((c, i) => {
          const tag = c.relation
            || (c.kind === "business" ? "Unternehmen" : c.kind === "person" ? "Person" : "");
          return (
            <button
              key={c.id}
              type="button"
              onClick={() => handleClick(c)}
              className="w-full text-left px-3 py-2 hover:bg-violet-500/[0.06] transition group"
            >
              <div className="flex items-center gap-2">
                <span className="text-[11px] text-muted-foreground tabular-nums shrink-0 w-7 text-right">{i + 1}.</span>
                <span className="text-sm font-medium flex-1 truncate">{c.display_name}</span>
                <span className="text-[10px] text-violet-500 opacity-0 group-hover:opacity-100 transition shrink-0">
                  {pickToChat ? "pick →" : "open →"}
                </span>
              </div>
              {tag && (
                <div className="text-[11px] text-muted-foreground mt-0.5 line-clamp-1 pl-9">
                  {tag}
                </div>
              )}
            </button>
          );
        })}
        {shown.length === 0 && q && (
          <div className="px-3 py-3 text-[11px] text-muted-foreground text-center">
            No matches for "{q}"
          </div>
        )}
      </div>
      {total > contacts.length && (
        <button
          type="button"
          onClick={() => navigate("/contacts")}
          className="w-full px-3 py-2 text-[11px] text-violet-500 hover:bg-violet-500/[0.06] border-t border-violet-500/10 text-left"
        >
          Open all {total} in /contacts →
        </button>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Photo result grid — Immich thumbnails inline below an assistant bubble
// ---------------------------------------------------------------------------

function photoToStashItem(p: PhotoHit): StashItem {
  // Same shape the lightbox "Send via Email" handoff used to build —
  // a Yorik proxy URL plus a best-guess filename and mimetype. The
  // backend stash endpoint enforces the /api/photos/ prefix.
  const filename = p.original_name || `photo-${p.id}.jpg`;
  const lc = filename.toLowerCase();
  const mimetype =
    lc.endsWith(".png")  ? "image/png"  :
    lc.endsWith(".webp") ? "image/webp" :
    lc.endsWith(".heic") ? "image/heic" :
    "image/jpeg";
  return {
    url: `/api/photos/${encodeURIComponent(p.id)}/raw`,
    filename,
    mimetype,
  };
}

function AttachToggle({
  attached, onClick, className,
}: {
  attached: boolean;
  onClick: (e: React.MouseEvent) => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onClick(e); }}
      className={cn(
        "rounded-full w-7 h-7 flex items-center justify-center shadow-md transition",
        attached
          ? "bg-violet-500 text-white hover:bg-violet-600"
          : "bg-black/60 text-white hover:bg-black/80",
        className,
      )}
      title={attached ? "Remove from email attachments" : "Attach to email"}
      aria-label={attached ? "Remove" : "Attach"}
    >
      {attached ? <Check className="w-3.5 h-3.5" /> : <Plus className="w-3.5 h-3.5" />}
    </button>
  );
}

function PhotoResultGrid({
  photos, onAttach, isAttached,
}: {
  photos: PhotoHit[];
  onAttach?: (item: StashItem) => void;
  isAttached?: (url: string, filename: string) => boolean;
}) {
  // First hit is the "top" pick — render it large; remaining as a
  // secondary strip so the user gets one clear answer with context.
  // The lightbox can walk through ALL photos via prev/next, so even
  // photos that don't fit in the strip are still reachable.
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null);
  if (photos.length === 0) return null;
  const [primary, ...rest] = photos;
  const VISIBLE_THUMBS = 6;
  const visibleRest = rest.slice(0, VISIBLE_THUMBS);
  const hidden = photos.length - 1 - visibleRest.length;

  function toggle(p: PhotoHit) {
    if (!onAttach) return;
    const item = photoToStashItem(p);
    // We don't have an explicit remove handler down here; "+ Attach"
    // is additive. The tray's per-item X removes. This matches the
    // simpler mental model: one button to add, the tray manages
    // removals.
    if (!isAttached?.(item.url, item.filename)) onAttach(item);
  }

  return (
    <div className="mt-3 max-w-md">
      <div className="relative">
        <button
          type="button"
          onClick={() => setLightboxIdx(0)}
          className="block w-full overflow-hidden rounded-xl border border-border bg-muted hover:border-violet-500/40 transition group"
        >
          <img
            src={primary.thumbnail_url}
            alt={primary.original_name || "Photo"}
            className="w-full h-auto max-h-[420px] object-cover group-hover:scale-[1.01] transition-transform"
            loading="lazy"
          />
        </button>
        {onAttach && (
          <AttachToggle
            attached={isAttached?.(photoToStashItem(primary).url, photoToStashItem(primary).filename) ?? false}
            onClick={() => toggle(primary)}
            className="absolute top-2 right-2"
          />
        )}
      </div>
      {visibleRest.length > 0 && (
        <div className="mt-1.5 grid grid-cols-6 gap-1.5">
          {visibleRest.map((p, i) => {
            // index in the full `photos` list is i+1 (primary is 0)
            const fullIdx = i + 1;
            const isLastSlot = i === visibleRest.length - 1 && hidden > 0;
            const item = photoToStashItem(p);
            const attached = isAttached?.(item.url, item.filename) ?? false;
            return (
              <div key={p.id} className="relative">
                <button
                  type="button"
                  // The last visible thumb doubles as the "+N more" entry
                  // point when extras exist — clicking it opens the
                  // lightbox right at the first unseen photo.
                  onClick={() =>
                    setLightboxIdx(isLastSlot ? fullIdx + 1 : fullIdx)
                  }
                  className="relative w-full aspect-square overflow-hidden rounded-md border border-border bg-muted hover:border-violet-500/40 transition block"
                  title={isLastSlot
                    ? `+${hidden} more`
                    : (p.original_name || "")}
                >
                  <img
                    src={p.thumbnail_url}
                    alt=""
                    className="w-full h-full object-cover"
                    loading="lazy"
                  />
                  {isLastSlot && (
                    <div className="absolute inset-0 bg-black/55 flex items-center justify-center text-white text-xs font-medium">
                      +{hidden}
                    </div>
                  )}
                </button>
                {/* Don't offer attach on the "+N more" slot — it isn't
                    a single photo, it's a navigation affordance. */}
                {onAttach && !isLastSlot && (
                  <AttachToggle
                    attached={attached}
                    onClick={() => toggle(p)}
                    className="absolute top-1 right-1 w-6 h-6"
                  />
                )}
              </div>
            );
          })}
        </div>
      )}
      {(photos[0].original_name || photos[0].taken_at) && (
        <div className="text-[10px] text-muted-foreground mt-1.5 truncate">
          {photos[0].original_name}
          {photos[0].taken_at && (
            <span className="opacity-70"> · {photos[0].taken_at.slice(0, 10)}</span>
          )}
          {photos.length > 1 && <span className="opacity-70"> · +{photos.length - 1} more</span>}
        </div>
      )}

      {lightboxIdx !== null && (
        <PhotoLightbox
          photos={photos}
          index={lightboxIdx}
          onChangeIndex={setLightboxIdx}
          onClose={() => setLightboxIdx(null)}
          onAttach={onAttach}
          isAttached={isAttached}
        />
      )}
    </div>
  );
}

function PhotoLightbox({
  photos, index, onChangeIndex, onClose, onAttach, isAttached,
}: {
  photos: PhotoHit[];
  index: number;
  onChangeIndex: (next: number) => void;
  onClose: () => void;
  onAttach?: (item: StashItem) => void;
  isAttached?: (url: string, filename: string) => boolean;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);

  // Clamp index so a stale value (e.g. after the list changes) doesn't crash.
  const safeIdx = Math.max(0, Math.min(index, photos.length - 1));
  const photo = photos[safeIdx];
  const hasPrev = safeIdx > 0;
  const hasNext = safeIdx < photos.length - 1;

  // Keyboard nav: ← → for prev/next, Esc closes. Bound to window so
  // it works regardless of focus inside the overlay.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "ArrowLeft"  && hasPrev) { e.preventDefault(); onChangeIndex(safeIdx - 1); }
      else if (e.key === "ArrowRight" && hasNext) { e.preventDefault(); onChangeIndex(safeIdx + 1); }
      else if (e.key === "Escape") { e.preventDefault(); onClose(); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [safeIdx, hasPrev, hasNext, onChangeIndex, onClose]);

  // Display uses Immich's pre-authed thumbnail (works as <img src> across
  // origin). All other actions route through Yorik's same-origin proxy
  // so Clipboard API and download attributes work — see /api/photos/{id}/raw.
  const displayUrl = photo.thumbnail_url;
  const rawUrl = `/api/photos/${encodeURIComponent(photo.id)}/raw`;
  const downloadUrl = `/api/photos/${encodeURIComponent(photo.id)}/raw?download=1`;

  function flash(msg: string) {
    setHint(msg);
    setTimeout(() => setHint(null), 1800);
  }

  async function copyToClipboard() {
    setBusy("copy");
    try {
      // Same-origin fetch through Yorik proxy → credentials flow via cookie.
      const r = await fetch(rawUrl, { credentials: "include" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      try {
        await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })]);
      } catch {
        // Some browsers refuse JPEG via Clipboard API — re-encode to PNG.
        const img = await blobToImage(blob);
        const png = await imageToPngBlob(img);
        await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
      }
      flash("Copied to clipboard");
    } catch (e: any) {
      flash("Copy failed: " + (e?.message || "unknown"));
    } finally {
      setBusy(null);
    }
  }

  function download() {
    // Same-origin + Content-Disposition: attachment from the proxy makes
    // the browser actually save instead of navigating.
    const a = document.createElement("a");
    a.href = downloadUrl;
    a.download = photo.original_name || `photo-${photo.id}.jpg`;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function shareToWhatsApp() {
    // Stash the photo URL in sessionStorage so the WhatsApp app can
    // pick it up and offer to attach it to a chat. We pass the proxy
    // URL so the banner's "Open image" link works without the user's
    // browser needing to talk to Immich directly.
    try {
      sessionStorage.setItem("yorik_share_photo", JSON.stringify({
        url: rawUrl, name: photo.original_name || "photo.jpg", from: "chat",
      }));
    } catch {}
    onClose();
    window.location.href = "/r/whatsapp";
  }

  function attachToEmailStash() {
    // Adds the current photo to the chat's conversation-bound stash;
    // the tray above the composer is where the user later hits
    // "Send via E-Mail". No navigation — they stay in the lightbox
    // and can swipe to the next photo to attach that one too.
    if (!onAttach) return;
    const item = photoToStashItem(photo);
    if (!isAttached?.(item.url, item.filename)) {
      onAttach(item);
      flash("Added to email attachments");
    } else {
      flash("Already attached");
    }
  }

  function openInPhotos() {
    onClose();
    // Pass the asset id so the Photos app can deep-link the iframe to
    // Immich's `/photos/<id>` view instead of dumping the user on the
    // timeline root.
    window.location.href = `/r/photos?asset=${encodeURIComponent(photo.id)}`;
  }

  return (
    <div
      className="fixed inset-0 z-[100] bg-black/85 backdrop-blur-sm flex items-center justify-center p-6 cursor-zoom-out"
      onClick={onClose}
    >
      {/* Prev / next arrows — fixed to the viewport edges so they don't
          shift when the image's aspect ratio changes between photos.
          stopPropagation keeps a click on the arrow from closing the
          lightbox. */}
      {hasPrev && (
        <button
          type="button"
          aria-label="Previous photo"
          onClick={(e) => { e.stopPropagation(); onChangeIndex(safeIdx - 1); }}
          className="absolute left-4 top-1/2 -translate-y-1/2 p-3 rounded-full bg-black/50 text-white hover:bg-black/70 transition cursor-pointer"
        >
          <ChevronLeft className="w-6 h-6" />
        </button>
      )}
      {hasNext && (
        <button
          type="button"
          aria-label="Next photo"
          onClick={(e) => { e.stopPropagation(); onChangeIndex(safeIdx + 1); }}
          className="absolute right-4 top-1/2 -translate-y-1/2 p-3 rounded-full bg-black/50 text-white hover:bg-black/70 transition cursor-pointer"
        >
          <ChevronRight className="w-6 h-6" />
        </button>
      )}
      {photos.length > 1 && (
        <div className="absolute top-4 left-1/2 -translate-x-1/2 text-xs text-white/85 bg-black/55 px-3 py-1 rounded-full">
          Photo {safeIdx + 1} of {photos.length}
        </div>
      )}

      <div
        className="relative max-w-full max-h-full flex flex-col items-center gap-3"
        onClick={e => e.stopPropagation()}
      >
        <img
          src={displayUrl}
          alt={photo.original_name || "Photo"}
          className="max-w-full max-h-[78vh] object-contain rounded-lg shadow-2xl cursor-default"
        />
        {/* Action bar */}
        <div className="bg-card/95 backdrop-blur border border-border rounded-xl shadow-2xl px-2 py-1.5 flex items-center gap-1 cursor-default flex-wrap justify-center max-w-[calc(100vw-2rem)]">
          <LbAction onClick={copyToClipboard} busy={busy === "copy"} title="Copy to clipboard">
            <Copy className="w-4 h-4" /> <span className="hidden sm:inline">Copy</span>
          </LbAction>
          <LbAction onClick={download} title="Download">
            <Download className="w-4 h-4" /> <span className="hidden sm:inline">Download</span>
          </LbAction>
          <LbAction onClick={shareToWhatsApp} title="Send via WhatsApp">
            <MessageSquare className="w-4 h-4" /> <span className="hidden sm:inline">WhatsApp</span>
          </LbAction>
          {onAttach && (
            <LbAction
              onClick={attachToEmailStash}
              title="Attach this photo to email"
            >
              {isAttached?.(photoToStashItem(photo).url, photoToStashItem(photo).filename)
                ? <><Check className="w-4 h-4" /> <span className="hidden sm:inline">Attached</span></>
                : <><Plus className="w-4 h-4" /> <span className="hidden sm:inline">Email</span></>}
            </LbAction>
          )}
          <LbAction onClick={openInPhotos} title="Open the Photos app">
            <Eye className="w-4 h-4" /> <span className="hidden sm:inline">Photos app</span>
          </LbAction>
        </div>
        {hint && (
          <div className="text-xs text-white/85 bg-black/60 px-3 py-1 rounded-full">{hint}</div>
        )}
        {(photo.original_name || photo.taken_at) && (
          <div className="text-[11px] text-white/60">
            {photo.original_name}
            {photo.taken_at && <span className="opacity-70"> · {photo.taken_at.slice(0, 10)}</span>}
          </div>
        )}
      </div>
    </div>
  );
}

function LbAction({ children, onClick, busy, title }: {
  children: React.ReactNode; onClick: () => void; busy?: boolean; title?: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={busy}
      title={title}
      aria-label={title}
      className="text-xs px-3 h-11 sm:h-auto sm:py-1.5 rounded-md hover:bg-muted transition disabled:opacity-50 inline-flex items-center gap-1.5"
    >
      {children}
    </button>
  );
}

async function blobToImage(blob: Blob): Promise<HTMLImageElement> {
  const u = URL.createObjectURL(blob);
  try {
    const img = new Image();
    img.src = u;
    await img.decode();
    return img;
  } finally {
    // We keep the URL alive briefly so the caller can finish encoding;
    // safe to revoke immediately because decoded raster is already
    // copied into the image bitmap.
    URL.revokeObjectURL(u);
  }
}

async function imageToPngBlob(img: HTMLImageElement): Promise<Blob> {
  const canvas = document.createElement("canvas");
  canvas.width = img.naturalWidth || img.width;
  canvas.height = img.naturalHeight || img.height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas 2D context unavailable");
  ctx.drawImage(img, 0, 0);
  return await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      b => b ? resolve(b) : reject(new Error("canvas.toBlob returned null")),
      "image/png",
    );
  });
}


// ---------------------------------------------------------------------------
// Document card — renders inline below an assistant bubble
// ---------------------------------------------------------------------------

function DocumentResultCard({
  doc, role, onAttach, isAttached,
}: {
  doc: DocumentHit;
  role: string;
  onAttach?: (item: StashItem) => void;
  isAttached?: (url: string, filename: string) => boolean;
}) {
  const [previewing, setPreviewing] = useState(false);
  // Paperless mirror rows tunnel through the Paperless reverse proxy;
  // local uploads through Yorik's own /api/documents/{id}/raw.
  // (Matches the routing in DocumentsApp.tsx → mediaUrlFor.)
  const isPaperless = doc.source === "paperless";
  const downloadUrl = isPaperless
    ? `/paperless/api/documents/${doc.doc_id}/download/`
    : `/api/documents/${doc.doc_id}/raw?role=${encodeURIComponent(role)}&download=1`;
  const previewUrl  = isPaperless
    ? `/paperless/api/documents/${doc.doc_id}/preview/`
    : `/api/documents/${doc.doc_id}/raw?role=${encodeURIComponent(role)}`;
  const Icon = pickDocIcon(doc.mime_type);

  return (
    <>
      <div className="bg-card border border-border rounded-xl p-3 hover:border-violet-500/40 hover:shadow-md transition">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-lg bg-violet-500/10 flex items-center justify-center shrink-0">
            <Icon className="w-5 h-5 text-violet-500" />
          </div>
          <div className="flex-1 min-w-0">
            {(() => {
              const { date, rest } = splitTitleDate(doc.title);
              return (
                <div className="flex items-center gap-2 min-w-0">
                  <div className="text-sm font-medium truncate flex-1 min-w-0" title={doc.title}>
                    {rest}
                  </div>
                  {date && (
                    <span
                      className="shrink-0 text-[10px] tabular-nums px-1.5 py-0.5 rounded bg-muted text-muted-foreground border border-border"
                      title="Document date (from Paperless)"
                    >
                      {date}
                    </span>
                  )}
                </div>
              );
            })()}
            {doc.snippet && (
              <div className="text-[11px] text-muted-foreground line-clamp-2 mt-0.5 leading-snug">
                {doc.snippet}
              </div>
            )}
            <div className="flex items-center gap-1.5 mt-2 flex-wrap">
              <button
                onClick={() => setPreviewing(true)}
                className="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded-md bg-muted hover:bg-muted/70 transition"
                title="Preview"
              >
                <Eye className="w-3 h-3" /> Preview
              </button>
              <a
                href={downloadUrl}
                className="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded-md bg-violet-500/10 hover:bg-violet-500/20 text-violet-500 transition"
                title="Download"
              >
                <Download className="w-3 h-3" /> Download
              </a>
              {onAttach && (() => {
                // Same URL the Download link uses, so a member who can
                // download can also email. Filename derives from the
                // doc title; mailers pick the right preview icon by
                // extension, so append one when missing.
                const mime = doc.mime_type || "application/octet-stream";
                const titleHasExt = /\.[A-Za-z0-9]{2,5}$/.test(doc.title || "");
                const ext = titleHasExt ? "" :
                  mime === "application/pdf" ? ".pdf" :
                  mime.startsWith("image/jpeg") ? ".jpg" :
                  mime.startsWith("image/png")  ? ".png" :
                  mime.startsWith("text/plain") ? ".txt" : "";
                const filename = (doc.title || `document-${doc.doc_id}`) + ext;
                const attached = isAttached?.(downloadUrl, filename) ?? false;
                return (
                  <button
                    onClick={() => {
                      if (!attached) onAttach({ url: downloadUrl, filename, mimetype: mime });
                    }}
                    className={cn(
                      "inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded-md transition",
                      attached
                        ? "bg-violet-500/20 text-violet-600 dark:text-violet-300"
                        : "hover:bg-muted/70 text-muted-foreground hover:text-foreground",
                    )}
                    title={attached ? "Already attached to email" : "Attach to email"}
                  >
                    {attached
                      ? <><Check className="w-3 h-3" /> Attached</>
                      : <><Plus className="w-3 h-3" /> Email</>}
                  </button>
                );
              })()}
              {/* User-driven nav into the Documents app. Replaces the
                  LLM auto-redirecting after every search — the user
                  decides when to leave the chat. */}
              <a
                href={`/r/documents?doc=${encodeURIComponent(doc.doc_id)}&source=${encodeURIComponent(doc.source || "paperless")}`}
                className="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded-md hover:bg-muted/70 transition text-muted-foreground hover:text-foreground"
                title="Open in Documents"
              >
                <FileText className="w-3 h-3" /> Open
              </a>
              {typeof doc.distance === "number" && (
                <span className="ml-auto text-[10px] text-muted-foreground" title="Cosine distance — lower is closer">
                  {doc.distance.toFixed(2)}
                </span>
              )}
            </div>
          </div>
        </div>
      </div>
      {previewing && (
        <DocumentPreviewModal doc={doc} src={previewUrl} onClose={() => setPreviewing(false)} />
      )}
    </>
  );
}

function DocumentPreviewModal({ doc, src, onClose }:
  { doc: DocumentHit; src: string; onClose: () => void }) {
  useEffect(() => {
    function esc(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  const mime = doc.mime_type || "";
  const isPdf   = mime.includes("pdf");
  const isImage = mime.startsWith("image/");
  const isText  = mime.startsWith("text/") || mime.includes("json") || mime.includes("markdown");

  return (
    <div
      className="fixed inset-0 z-[1000] bg-black/60 backdrop-blur-sm flex items-center justify-center p-6"
      onClick={onClose}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-4xl h-[80vh] flex flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-3 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-3 min-w-0">
            <FileText className="w-4 h-4 text-violet-500" />
            <span className="font-medium truncate">{doc.title}</span>
          </div>
          <div className="flex items-center gap-2">
            <a
              href={src + "&download=1"}
              className="inline-flex items-center gap-1 text-xs px-3 py-1.5 rounded-md bg-violet-500/10 hover:bg-violet-500/20 text-violet-500 transition"
            >
              <Download className="w-3 h-3" /> Download
            </a>
            <button
              onClick={onClose}
              className="w-10 h-10 md:w-7 md:h-7 rounded-md hover:bg-muted text-muted-foreground flex items-center justify-center"
              title="Close (Esc)"
              aria-label="Close"
            >
              <X className="w-5 h-5 md:w-4 md:h-4" />
            </button>
          </div>
        </header>
        <div className="flex-1 overflow-hidden bg-muted/30">
          {isPdf && (
            <iframe src={src} className="w-full h-full" title={doc.title} />
          )}
          {isImage && (
            <div className="w-full h-full flex items-center justify-center p-4">
              <img src={src} alt={doc.title} className="max-w-full max-h-full object-contain rounded" />
            </div>
          )}
          {isText && (
            <iframe src={src} className="w-full h-full bg-white" title={doc.title} />
          )}
          {!isPdf && !isImage && !isText && (
            <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-3">
              <FileText className="w-12 h-12 opacity-30" />
              <div>No inline preview for {mime || "this file type"}.</div>
              <a
                href={src + "&download=1"}
                className="inline-flex items-center gap-2 text-sm px-4 py-2 rounded-lg bg-violet-500 text-white hover:bg-violet-600 transition"
              >
                <Download className="w-4 h-4" /> Download
              </a>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty state + starter prompts
// ---------------------------------------------------------------------------

function EmptyThread({ onStartNew }: { onStartNew: () => void }) {
  return (
    <div className="flex-1 flex flex-col items-center justify-center text-center px-6">
      <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-violet-500/30 to-blue-500/30 flex items-center justify-center mb-5">
        <Sparkles className="w-7 h-7 text-violet-500" />
      </div>
      <div className="font-semibold text-lg">Yorik Chat</div>
      <div className="text-sm text-muted-foreground mt-1 max-w-md">
        Ask about your calendar, tasks, bills, or any document you've uploaded.
        Yorik can also write to the database — schedule events, mark bills paid, add tasks.
      </div>
      <button
        onClick={onStartNew}
        className="mt-6 inline-flex items-center gap-2 px-4 py-2 rounded-full bg-violet-500 hover:bg-violet-600 text-white text-sm shadow-md transition"
      >
        <Plus className="w-4 h-4" /> Start a conversation
      </button>
    </div>
  );
}

function ConversationStarter({ onPick }: { onPick: (s: string) => void }) {
  // Retained for back-compat with anywhere else that still imports it.
  // The chat thread itself now uses <TodayDigest> for the empty state.
  const suggestions = [
    "What's on my calendar this week?",
    "Find my insurance policy",
    "Add 'Pick up dry cleaning' to my tasks",
    "How many unpaid bills are due this month?",
  ];
  return (
    <div className="py-8">
      <div className="text-xs text-muted-foreground text-center mb-3">Try one of these:</div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-w-2xl mx-auto">
        {suggestions.map(s => (
          <button
            key={s}
            onClick={() => onPick(s)}
            className="text-left text-sm bg-card border border-border rounded-xl px-4 py-3 hover:border-violet-500/40 hover:shadow-md transition"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}


// ─── ToolTraceSummary ───────────────────────────────────────────────
// One-line ambient hint showing which tools ran for an assistant turn
// ("📞 find_contact · 🗓 add_calendar_event"). Click to expand the
// full args + result per call. Always on (no dev-mode gate) —
// transparency is the trust signal we want users to see by default.
const TOOL_ICONS: Record<string, string> = {
  find_contact:               "📞",
  add_contact:                "📞",
  add_calendar_event:         "🗓",
  update_calendar_event:      "🗓",
  delete_calendar_event:      "🗓",
  check_calendar:             "🗓",
  block_travel_time:          "🚗",
  calculate_travel_time:      "🚗",
  add_task:                   "✅",
  check_tasks:                "✅",
  search_documents:           "📄",
  find_document:              "📄",
  find_photo:                 "📷",
  compose_draft:              "✉️",
  web_search:                 "🔍",
  web_extract:                "🌐",
  find_known_provider:        "🏥",
  find_provider_nearby:       "🏥",
  list_contacts_for_picking:  "👥",
};

function toolIcon(name: string): string {
  return TOOL_ICONS[name] || "🔧";
}

function ToolTraceSummary({ entries }: { entries: ToolTraceEntry[] }) {
  const [open, setOpen] = useState(false);
  const summary = entries
    .map(e => `${toolIcon(e.name)} ${e.name}`)
    .join(" · ");
  return (
    <div className="mt-1.5 text-[10px] text-muted-foreground">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="inline-flex items-center gap-1 hover:text-foreground transition"
        title={open ? "Hide tool details" : "Show what Yorik did"}
      >
        {open
          ? <ChevronDown className="w-2.5 h-2.5" />
          : <ChevronRight className="w-2.5 h-2.5" />}
        <Wrench className="w-2.5 h-2.5 opacity-70" />
        <span className="font-mono">{summary}</span>
      </button>
      {open && (
        <div className="mt-1 pl-3 ml-1 border-l border-border/60 space-y-1.5">
          {entries.map((e, i) => (
            <div key={i} className="font-mono text-[10px] leading-relaxed">
              <div className="text-foreground/80">
                {toolIcon(e.name)} <span className="font-semibold">{e.name}</span>
                <span className="opacity-70">
                  ({(() => {
                    try {
                      const s = JSON.stringify(e.args ?? {});
                      return s.length > 200 ? s.slice(0, 200) + "…" : s;
                    } catch { return "{}"; }
                  })()})
                </span>
              </div>
              {e.result && (
                <div className="ml-4 opacity-60 whitespace-pre-wrap break-words">
                  ← {e.result.length > 200 ? e.result.slice(0, 200) + "…" : e.result}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


// ─── TodayDigest ────────────────────────────────────────────────────
// Replaces the static 4-chip ConversationStarter on the empty-thread
// screen. Pulls /api/today (events, overdue tasks, pending contacts,
// upcoming birthdays) and renders each as a clickable card that
// seeds the composer with a real, contextual question.
//
// Falls back gracefully to the generic chips when the digest fetch
// fails or returns nothing actionable — first-day installs should
// still see something useful.

interface TodayDigestData {
  today_date: string;
  events_today: Array<{
    id: number;
    title: string;
    starts_at: string;
    ends_at: string | null;
    all_day: number;
    location?: string | null;
  }>;
  tasks_overdue_count: number;
  tasks_overdue_sample: Array<{ id: number; title: string; due_date: string }>;
  contacts_pending_count: number;
  birthdays_this_week: Array<{
    id: number; display_name: string; birthday: string; days_away: number;
  }>;
  saved_query_count: number;
}

function TodayDigest({ onPick }: { onPick: (s: string) => void }) {
  const digest = useApi<TodayDigestData>("/api/today", []);
  const d = digest.data;

  // Render rules: count something only if there's signal. If all
  // sections are empty, fall back to the generic ConversationStarter
  // so first-day users still get a prompt.
  const hasSignal = !!d && (
    d.events_today.length > 0
    || d.tasks_overdue_count > 0
    || d.contacts_pending_count > 0
    || d.birthdays_this_week.length > 0
  );

  if (digest.loading) {
    return (
      <div className="py-8 text-center text-xs text-muted-foreground">
        <Loader2 className="w-4 h-4 animate-spin inline mr-2" /> Looking at your day…
      </div>
    );
  }
  if (!hasSignal) {
    return <ConversationStarter onPick={onPick} />;
  }

  const greeting = (() => {
    const h = new Date().getHours();
    if (h < 12) return "Good morning";
    if (h < 18) return "Good afternoon";
    return "Good evening";
  })();

  return (
    <div className="py-6">
      <div className="text-xs text-muted-foreground text-center mb-4">
        {greeting} — here's your day.
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-w-2xl mx-auto">
        {d!.events_today.length > 0 && (
          <DigestCard
            icon={<Calendar className="w-4 h-4 text-violet-500" />}
            title={`${d!.events_today.length} event${d!.events_today.length === 1 ? "" : "s"} today`}
            subtitle={d!.events_today.slice(0, 2).map(e => {
              const t = e.starts_at?.slice(11, 16);
              return `${t} ${e.title}`;
            }).join(" · ")}
            onClick={() => onPick("What's on today?")}
          />
        )}
        {d!.tasks_overdue_count > 0 && (
          <DigestCard
            icon={<CheckSquare className="w-4 h-4 text-amber-500" />}
            title={`${d!.tasks_overdue_count} overdue task${d!.tasks_overdue_count === 1 ? "" : "s"}`}
            subtitle={d!.tasks_overdue_sample.slice(0, 2).map(t => t.title).join(" · ")}
            onClick={() => onPick("Which tasks are overdue?")}
            tone="amber"
          />
        )}
        {d!.contacts_pending_count > 0 && (
          <DigestCard
            icon={<UsersRound className="w-4 h-4 text-rose-500" />}
            title={`${d!.contacts_pending_count} contact${d!.contacts_pending_count === 1 ? "" : "s"} waiting for review`}
            subtitle="From email, WhatsApp, or vCard import"
            onClick={() => onPick("Show me the pending contacts.")}
          />
        )}
        {d!.birthdays_this_week.length > 0 && (
          <DigestCard
            icon={<Cake className="w-4 h-4 text-pink-500" />}
            title={
              d!.birthdays_this_week.length === 1
                ? `${d!.birthdays_this_week[0].display_name}'s birthday ${d!.birthdays_this_week[0].days_away === 0 ? "is today" : `is in ${d!.birthdays_this_week[0].days_away} day${d!.birthdays_this_week[0].days_away === 1 ? "" : "s"}`}`
                : `${d!.birthdays_this_week.length} birthdays this week`
            }
            subtitle={d!.birthdays_this_week.slice(0, 2).map(b =>
              `${b.display_name}${b.days_away === 0 ? " (today)" : ""}`
            ).join(" · ")}
            onClick={() => {
              const first = d!.birthdays_this_week[0];
              onPick(`Write ${first.display_name} a short birthday note.`);
            }}
          />
        )}
      </div>
      <div className="text-[10px] text-muted-foreground/70 text-center mt-4">
        Or just ask something else ↓
      </div>
    </div>
  );
}

function DigestCard({
  icon, title, subtitle, onClick, tone,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  onClick: () => void;
  tone?: "amber";
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "text-left bg-card border rounded-xl px-3.5 py-3 hover:shadow-md transition flex items-start gap-2.5",
        tone === "amber"
          ? "border-amber-500/30 hover:border-amber-500/60"
          : "border-border hover:border-violet-500/40",
      )}
    >
      <div className="shrink-0 mt-0.5">{icon}</div>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium leading-snug">{title}</div>
        {subtitle && (
          <div className="text-[11px] text-muted-foreground mt-0.5 truncate">
            {subtitle}
          </div>
        )}
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function TurnFeedback({ conversationId, messageIdx }:
  { conversationId: string | null; messageIdx: number }) {
  const [rated, setRated] = useState<1 | -1 | null>(null);
  const [busy, setBusy] = useState(false);

  async function rate(value: 1 | -1) {
    if (busy || rated === value) return;
    setBusy(true);
    try {
      await api.post("/api/feedback/turn", {
        conversation_id: conversationId,
        message_idx: messageIdx,
        rating: value,
      });
      setRated(value);
    } catch {
      // Silent fail — feedback is best-effort
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={cn(
      "mt-1.5 flex items-center gap-1 transition-opacity",
      rated ? "opacity-100" : "opacity-0 group-hover:opacity-100",
    )}>
      <button
        onClick={() => rate(1)}
        disabled={busy}
        title="This was helpful"
        className={cn(
          "w-6 h-6 rounded-md flex items-center justify-center transition",
          rated === 1
            ? "bg-emerald-500/15 text-emerald-600"
            : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
        )}
      >
        <ThumbsUp className="w-3 h-3" />
      </button>
      <button
        onClick={() => rate(-1)}
        disabled={busy}
        title="This wasn't helpful"
        className={cn(
          "w-6 h-6 rounded-md flex items-center justify-center transition",
          rated === -1
            ? "bg-red-500/15 text-red-500"
            : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
        )}
      >
        <ThumbsDown className="w-3 h-3" />
      </button>
      {rated && (
        <span className="text-[10px] text-muted-foreground ml-1">
          {rated === 1 ? "Thanks!" : "Logged · we'll improve it"}
        </span>
      )}
    </div>
  );
}

function pickDocIcon(mime?: string | null) {
  // Lucide doesn't ship every doc icon; keep this list lean.
  // FileText covers most office formats reasonably well.
  return FileText;
}

// Paperless renames every ingested doc to "{YYYY-MM-DD} {original title}"
// from its OCR'd document date — see the user's setup. Reading the date
// prefix on every chat row eats scannability; pull it out so the title
// leads and the date becomes a small chip. Falls through unchanged
// for titles that don't match the pattern (graceful when imports come
// from elsewhere). Pure display split — the underlying doc.title is
// preserved on the hover tooltip so power users still see the literal
// Paperless title, and the LLM continues to see the dated title in
// search_documents tool results.
function splitTitleDate(title: string): { date: string | null; rest: string } {
  if (!title) return { date: null, rest: title };
  const m = title.match(/^(\d{4})-(\d{2})-(\d{2})\s+(.+)$/);
  if (!m) return { date: null, rest: title };
  return { date: `${m[3]}.${m[2]}.${m[1]}`, rest: m[4] };
}

function formatRelative(iso: string): string {
  const d = new Date(iso.replace(" ", "T") + (iso.includes("T") ? "" : "Z"));
  if (isNaN(d.getTime())) return iso;
  const diffMin = (Date.now() - d.getTime()) / 60_000;
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${Math.round(diffMin)}m ago`;
  if (diffMin < 24 * 60) return `${Math.round(diffMin / 60)}h ago`;
  if (diffMin < 7 * 24 * 60) return `${Math.round(diffMin / (24 * 60))}d ago`;
  return d.toLocaleDateString([], { day: "numeric", month: "short" });
}


/** Bucket conversations into the sidebar's grouped layout.
 *  Returns an ordered list of [label, items] tuples — Pinned first
 *  (when any), then Today / Yesterday / Last 7 days / Earlier based
 *  on updated_at. Empty groups are dropped so the sidebar doesn't
 *  show headers with nothing under them. */
function groupConversations(
  list: ConversationSummary[],
): Array<[string, ConversationSummary[]]> {
  const pinned: ConversationSummary[]   = [];
  const today: ConversationSummary[]    = [];
  const yesterday: ConversationSummary[] = [];
  const lastWeek: ConversationSummary[] = [];
  const earlier: ConversationSummary[]  = [];

  const now = new Date();
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfToday.getDate() - 1);
  const startOfWeek = new Date(startOfToday);
  startOfWeek.setDate(startOfToday.getDate() - 7);

  for (const c of list) {
    if (c.pinned) { pinned.push(c); continue; }
    const d = new Date((c.updated_at || "").replace(" ", "T"));
    if (isNaN(d.getTime())) { earlier.push(c); continue; }
    if (d >= startOfToday)       today.push(c);
    else if (d >= startOfYesterday) yesterday.push(c);
    else if (d >= startOfWeek)   lastWeek.push(c);
    else                         earlier.push(c);
  }

  const out: Array<[string, ConversationSummary[]]> = [];
  if (pinned.length)    out.push(["Pinned", pinned]);
  if (today.length)     out.push(["Today", today]);
  if (yesterday.length) out.push(["Yesterday", yesterday]);
  if (lastWeek.length)  out.push(["Last 7 days", lastWeek]);
  if (earlier.length)   out.push(["Earlier", earlier]);
  return out;
}

// ---------------------------------------------------------------------------
// Debug bundle modal
//
// User-initiated, paste-anywhere export of one conversation for bug
// reports. The backend (backend/debug_bundle.py) auto-redacts well-
// shaped tokens (emails, phones, IBANs, IPs, secrets); names and
// free-text content are NOT auto-stripped — the user must review the
// pretty-printed JSON in the textarea and edit before sharing.
// Yorik never sends the bundle anywhere; Copy / Download are the
// only egress paths and they're both user-driven.
// ---------------------------------------------------------------------------

type DebugBundleResponse = {
  conversation_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  messages: any[];
  environment: Record<string, any>;
  redaction: {
    applied: boolean;
    counts: Record<string, number>;
    notes: string;
  };
};

function DebugBundleModal({
  conversation,
  onClose,
}: {
  conversation: ConversationSummary;
  onClose: () => void;
}) {
  const [bundle, setBundle] = useState<DebugBundleResponse | null>(null);
  const [editedJson, setEditedJson] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .get<DebugBundleResponse>(
        `/api/debug-bundle?conversation_id=${encodeURIComponent(conversation.id)}`,
      )
      .then(b => {
        if (cancelled) return;
        setBundle(b);
        setEditedJson(JSON.stringify(b, null, 2));
      })
      .catch(e => {
        if (cancelled) return;
        setError(String(e?.message || "failed to build bundle"));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [conversation.id]);

  function doCopy() {
    navigator.clipboard.writeText(editedJson).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      },
      () => setError("Copy to clipboard failed"),
    );
  }

  function doDownload() {
    const blob = new Blob([editedJson], { type: "application/json" });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    const safeTitle = (conversation.title || conversation.id)
      .replace(/[^A-Za-z0-9_\-]+/g, "_").slice(0, 40);
    a.href     = url;
    a.download = `yorik-debug-${safeTitle}-${conversation.id.slice(0, 8)}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  const counts = bundle?.redaction?.counts || {};
  const redactedTotal = Object.values(counts).reduce((s, n) => s + (n || 0), 0);

  return (
    <div
      className="fixed inset-0 z-[1200] bg-black/50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-card text-card-foreground border border-border rounded-xl shadow-2xl w-full max-w-3xl max-h-[90vh] flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-3 border-b border-border flex items-start gap-3">
          <div className="w-9 h-9 rounded-md bg-blue-500/10 flex items-center justify-center shrink-0">
            <Bug className="w-4 h-4 text-blue-500" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="font-semibold text-sm">Export debug bundle</div>
            <div className="text-xs text-muted-foreground truncate">
              {conversation.title || conversation.id}
            </div>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center justify-center"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="px-5 py-3 text-xs border-b border-border space-y-2 bg-muted/30">
          <div className="flex items-start gap-2">
            <AlertCircle className="w-3.5 h-3.5 mt-0.5 text-amber-600 shrink-0" />
            <div className="text-muted-foreground leading-snug">
              <strong className="text-foreground">Review before sharing.</strong>{" "}
              Auto-redaction strips well-shaped tokens (emails, phone numbers,
              IBANs, IPv4 addresses, API keys / passwords / bearer tokens /
              bcrypt hashes). It does <strong>not</strong> strip names,
              free-text addresses, OCR content, or message text — edit those
              out by hand below if you don't want to share them.
            </div>
          </div>
          {bundle && (
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <span className="text-muted-foreground">Auto-redacted:</span>
              {redactedTotal === 0 ? (
                <span className="text-muted-foreground italic">
                  nothing matched (didn't find any obvious PII tokens)
                </span>
              ) : (
                Object.entries(counts).map(([k, n]) => (
                  <span
                    key={k}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border border-emerald-500/20 font-mono"
                  >
                    {k}: {n}
                  </span>
                ))
              )}
            </div>
          )}
        </div>

        <div className="flex-1 min-h-0 overflow-hidden p-4">
          {loading && (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              <Loader2 className="w-5 h-5 animate-spin" />
            </div>
          )}
          {error && (
            <div className="text-sm text-red-500 p-4 rounded-md bg-red-500/5 border border-red-500/20">
              {error}
            </div>
          )}
          {bundle && !error && (
            <textarea
              value={editedJson}
              onChange={e => setEditedJson(e.target.value)}
              spellCheck={false}
              className="w-full h-full font-mono text-[11px] leading-snug p-3 rounded-md bg-muted/40 border border-border focus:outline-none focus:ring-2 focus:ring-ring/30 resize-none"
            />
          )}
        </div>

        <footer className="px-5 py-3 border-t border-border flex items-center justify-between gap-3 bg-muted/20">
          <div className="text-[11px] text-muted-foreground">
            {bundle ? `${bundle.message_count} messages · ${(editedJson.length / 1024).toFixed(1)} KB` : ""}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="px-3 py-1.5 rounded-md text-sm hover:bg-muted text-muted-foreground transition"
            >
              Cancel
            </button>
            <button
              onClick={doDownload}
              disabled={!bundle || !!error}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm border border-border hover:bg-muted disabled:opacity-50 disabled:cursor-not-allowed transition"
            >
              <Download className="w-3.5 h-3.5" />
              Download .json
            </button>
            <button
              onClick={doCopy}
              disabled={!bundle || !!error}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed transition"
            >
              {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
              {copied ? "Copied" : "Copy to clipboard"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}

