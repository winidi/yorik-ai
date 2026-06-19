/**
 * Floating bottom-right pill — shown whenever the document bucket has
 * at least one doc. Click "Chat about this" → builds a chat seed
 * listing each bucketed doc by id+title, drops it into the chat
 * composer (without auto-sending) and navigates to /chat.
 *
 * The seed format follows the hybrid strategy: filenames + IDs only,
 * NO inline text. The LLM calls read_document(doc_id=<n>) on demand
 * when it actually needs to read a doc — keeps the seed cheap on
 * context budget regardless of how many docs are bucketed.
 */

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { FolderOpen, MessageSquare, X, Loader2 } from "lucide-react";
import { useDocBucket } from "@/apps/documents/DocBucketContext";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface DocMeta {
  id: number;
  title: string;
  mime_type?: string | null;
  chunk_count?: number;
  bytes?: number;
  created_at?: string;
  source?: string;
}

export function DocBucketPill() {
  const { ids, clear, remove } = useDocBucket();
  const navigate = useNavigate();
  const [expanded, setExpanded] = useState(false);
  const [docs, setDocs] = useState<DocMeta[]>([]);
  const [busy, setBusy] = useState(false);

  // Resolve titles for the current bucket whenever it changes. Cheap —
  // /api/documents/{id} is a single SQLite lookup per doc.
  useEffect(() => {
    if (ids.length === 0) { setDocs([]); return; }
    let alive = true;
    Promise.all(ids.map(id =>
      api.get<DocMeta>(`/api/documents/${id}`).catch(() => null)
    )).then(results => {
      if (!alive) return;
      setDocs(results.filter((r): r is DocMeta => r !== null));
    });
    return () => { alive = false; };
  }, [ids]);

  if (ids.length === 0) return null;

  async function startChat() {
    setBusy(true);
    try {
      const lines = docs.map(d => {
        const kind = (d.mime_type || "").split("/").pop()?.toUpperCase() || "doc";
        const date = (d.created_at || "").slice(0, 10);
        return `#${d.id} ${d.title} (${kind}${date ? " · " + date : ""})`;
      });
      const seed =
        `[Document context — ${docs.length} document${docs.length === 1 ? "" : "s"} in the bucket]\n\n` +
        lines.join("\n") +
        `\n\nTip: for the full text of one of these docs, ` +
        `call read_document(doc_id=<n>).\n\n---\n\n`;
      try { sessionStorage.setItem("yorik_chat_seed", seed); } catch {}
      navigate("/chat");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed bottom-6 right-6 z-[55]">
      {expanded && (
        <div className="mb-2 w-[300px] max-h-[50vh] overflow-y-auto rounded-xl border border-border bg-card shadow-2xl">
          <div className="px-3 py-2 border-b border-border flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Bucket · {ids.length}
            </span>
            <button
              type="button"
              onClick={() => { clear(); setExpanded(false); }}
              className="text-[11px] text-rose-500 hover:underline"
            >
              clear all
            </button>
          </div>
          <ul className="divide-y divide-border/60">
            {docs.map(d => (
              <li key={d.id} className="px-3 py-2 flex items-start gap-2 text-xs">
                <span className="flex-1 min-w-0 truncate" title={d.title}>
                  {d.title}
                </span>
                <button
                  type="button"
                  onClick={() => remove(d.id)}
                  className="text-muted-foreground hover:text-rose-500 shrink-0"
                  title="Remove from bucket"
                >
                  <X className="w-3 h-3" />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex items-stretch gap-px rounded-full overflow-hidden shadow-2xl border border-violet-500/40">
        <button
          type="button"
          onClick={() => setExpanded(e => !e)}
          className={cn(
            "h-11 pl-4 pr-3 flex items-center gap-2 text-sm font-medium transition",
            "bg-violet-500/15 hover:bg-violet-500/25 text-violet-700 dark:text-violet-300",
          )}
          title={expanded ? "Hide list" : "Show bucketed documents"}
        >
          <FolderOpen className="w-4 h-4" />
          <span>{ids.length} doc{ids.length === 1 ? "" : "s"}</span>
        </button>
        <button
          type="button"
          onClick={startChat}
          disabled={busy || docs.length === 0}
          className={cn(
            "h-11 pl-3 pr-4 flex items-center gap-2 text-sm font-medium transition",
            "bg-violet-500 hover:bg-violet-600 text-white disabled:opacity-60",
          )}
          title="Open chat seeded with these documents"
        >
          {busy
            ? <Loader2 className="w-4 h-4 animate-spin" />
            : <MessageSquare className="w-4 h-4" />}
          Chat about this
        </button>
      </div>
    </div>
  );
}
