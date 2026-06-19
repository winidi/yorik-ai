/**
 * Per-conversation attachment stash UI.
 *
 * Lives above the chat composer and shows the {url, filename, mimetype}
 * triples the user has accumulated by clicking "+ Attach" on photo/
 * document cards. "Send via email" hands the array to the email
 * Composer via the existing yorik_pending_email sessionStorage key —
 * Composer.tsx already fetches each URL and MIME-attaches it on send.
 *
 * State lives on the server (agent_conversations.attachment_stash)
 * so the stash survives reloads and follows the conversation back
 * when the user reopens it later.
 */

import { useCallback, useEffect, useState } from "react";
import { Mail, X, Paperclip, Loader2 } from "lucide-react";
import { api } from "@/lib/api";

export interface StashItem {
  url:      string;
  filename: string;
  mimetype?: string;
}

interface StashResponse {
  items: StashItem[];
}

/** Server-backed stash hook. Returns the list plus mutators that
 *  optimistically update the UI before the round-trip completes. */
export function useAttachmentStash(conversationId: string | null) {
  const [items, setItems] = useState<StashItem[]>([]);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!conversationId) {
      setItems([]);
      return;
    }
    try {
      setLoading(true);
      const r = await api.get<StashResponse>(
        `/api/conversations/${encodeURIComponent(conversationId)}/stash`,
      );
      setItems(r.items || []);
    } catch {
      // 404 = conversation doesn't exist yet (no first turn saved).
      // 403 = wrong role. Either way, show empty.
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => { refresh(); }, [refresh]);

  const add = useCallback(async (item: StashItem) => {
    if (!conversationId) return;
    // Optimistic — drop the in-memory dedupe so the UI updates
    // instantly. Backend dedupes by (url, filename) so re-posting is
    // safe.
    setItems(prev =>
      prev.some(i => i.url === item.url && i.filename === item.filename)
        ? prev
        : [...prev, item]
    );
    try {
      const r = await api.post<StashResponse>(
        `/api/conversations/${encodeURIComponent(conversationId)}/stash`,
        item,
      );
      setItems(r.items || []);
    } catch {
      // Roll back on failure.
      refresh();
    }
  }, [conversationId, refresh]);

  const remove = useCallback(async (index: number) => {
    if (!conversationId) return;
    setItems(prev => prev.filter((_, i) => i !== index));
    try {
      const r = await api.delete<StashResponse>(
        `/api/conversations/${encodeURIComponent(conversationId)}/stash/${index}`,
      );
      setItems(r.items || []);
    } catch {
      refresh();
    }
  }, [conversationId, refresh]);

  const clear = useCallback(async () => {
    if (!conversationId) return;
    setItems([]);
    try {
      await api.delete(`/api/conversations/${encodeURIComponent(conversationId)}/stash`);
    } catch {
      refresh();
    }
  }, [conversationId, refresh]);

  const has = useCallback((url: string, filename: string) =>
    items.some(i => i.url === url && i.filename === filename),
    [items],
  );

  return { items, loading, add, remove, clear, has, refresh };
}


interface TrayProps {
  items: StashItem[];
  onRemove: (index: number) => void;
  onClear: () => void;
}

/** The dismissible bar above the chat composer. Hidden when the stash
 *  is empty so it doesn't take up vertical space for users who never
 *  attach anything. */
export function AttachmentStashTray({ items, onRemove, onClear }: TrayProps) {
  const [sending, setSending] = useState(false);

  if (items.length === 0) return null;

  function sendAll() {
    setSending(true);
    try {
      // Same shape Composer reads from yorik_pending_email — array of
      // {url, filename, mimetype}. EmailApp clears the key after read.
      sessionStorage.setItem("yorik_pending_email", JSON.stringify({
        attachments: items.map(i => ({
          url: i.url,
          filename: i.filename,
          mimetype: i.mimetype || "application/octet-stream",
        })),
      }));
    } catch {}
    window.location.href = "/r/email";
  }

  return (
    <div className="mb-2 rounded-2xl border border-violet-500/30 bg-violet-500/[0.06] px-3 py-2">
      <div className="flex items-center gap-2 mb-1.5">
        <Paperclip className="w-3.5 h-3.5 text-violet-500" />
        <div className="text-[11px] font-medium text-foreground/80">
          {items.length} {items.length === 1 ? "attachment" : "attachments"} ready to send
        </div>
        <div className="flex-1" />
        <button
          type="button"
          onClick={onClear}
          className="text-[11px] text-muted-foreground hover:text-foreground transition px-1.5 py-0.5 rounded"
        >
          Clear all
        </button>
        <button
          type="button"
          onClick={sendAll}
          disabled={sending}
          className="text-[11px] inline-flex items-center gap-1 px-2.5 py-1 rounded-md bg-violet-500 hover:bg-violet-600 text-white shadow-sm transition disabled:opacity-50"
        >
          {sending
            ? <><Loader2 className="w-3 h-3 animate-spin" /> Opening…</>
            : <><Mail className="w-3 h-3" /> Send via email</>}
        </button>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {items.map((it, i) => (
          <span
            key={`${it.url}-${i}`}
            className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-background border border-border max-w-[200px]"
            title={it.filename}
          >
            <span className="truncate">{it.filename}</span>
            <button
              type="button"
              onClick={() => onRemove(i)}
              className="text-muted-foreground hover:text-foreground shrink-0"
              aria-label="Remove"
            >
              <X className="w-3 h-3" />
            </button>
          </span>
        ))}
      </div>
    </div>
  );
}
