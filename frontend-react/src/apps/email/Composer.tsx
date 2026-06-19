/**
 * Compose modal — floating bottom-right (Gmail style). Used for both
 * fresh emails and replies. Reply pre-fills To + Subject + In-Reply-To
 * so the recipient's client renders it as a thread continuation.
 *
 * Upgrades in this revision:
 *   - **TipTap rich-text body** (bold/italic/underline/link/OL/UL/quote)
 *     instead of plain `<textarea>`. We send both body_text + body_html
 *     so plain-text clients still get a readable copy.
 *   - **Drag-drop attachments** into the editor area. Files get
 *     base64-encoded client-side and POSTed alongside the message.
 *     25MB total cap matches the backend.
 *   - **Recipient autocomplete** from contacts via /api/chat/mentions
 *     (same endpoint the chat @-mention popover uses).
 *   - **Autosave** to localStorage every few seconds so closing the
 *     composer mid-draft doesn't lose work. Cleared on send.
 *   - ⌘/Ctrl+Enter sends.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  X, Send, Loader2, Minus, AlertCircle, UsersRound,
  Bold as BoldIcon, Italic as ItalicIcon, Underline as UnderlineIcon,
  Link as LinkIcon, List as ListIcon, ListOrdered, Quote, Paperclip,
  Sparkles,
} from "lucide-react";
import { EditorContent, useEditor } from "@tiptap/react";
import type { Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Underline } from "@tiptap/extension-underline";
import { Link } from "@tiptap/extension-link";
import { Placeholder } from "@tiptap/extension-placeholder";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { cn } from "@/lib/utils";
import type { EmailAccount } from "./types";

// Per-tone tint — mirrors AIDraftPanel's EMAIL_TONE_TINTS so the
// new-compose draft panel feels like the reply panel.
const TONE_TINTS: Record<string, { idle: string; active: string }> = {
  friendly: {
    idle:   "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/15",
    active: "bg-emerald-500/20 text-emerald-700 dark:text-emerald-200 ring-1 ring-emerald-500/40",
  },
  formal: {
    idle:   "bg-slate-500/10 text-slate-700 dark:text-slate-300 hover:bg-slate-500/15",
    active: "bg-slate-500/20 text-slate-700 dark:text-slate-200 ring-1 ring-slate-500/40",
  },
  quick: {
    idle:   "bg-sky-500/10 text-sky-700 dark:text-sky-300 hover:bg-sky-500/15",
    active: "bg-sky-500/20 text-sky-700 dark:text-sky-200 ring-1 ring-sky-500/40",
  },
  warm: {
    idle:   "bg-rose-500/10 text-rose-700 dark:text-rose-300 hover:bg-rose-500/15",
    active: "bg-rose-500/20 text-rose-700 dark:text-rose-200 ring-1 ring-rose-500/40",
  },
  firm: {
    idle:   "bg-amber-500/10 text-amber-700 dark:text-amber-300 hover:bg-amber-500/15",
    active: "bg-amber-500/20 text-amber-700 dark:text-amber-200 ring-1 ring-amber-500/40",
  },
};
const TONE_TINT_DEFAULT = {
  idle:   "bg-muted/60 text-foreground/85 hover:bg-muted",
  active: "bg-primary/15 text-primary ring-1 ring-primary/40",
};

export interface ComposeDraft {
  accountId?: number;
  to: string;
  cc?: string;
  subject: string;
  /** Plain text OR HTML — auto-detected on open. New composer always
   *  starts blank; reply prefills come in as plain text and TipTap
   *  paragraph-wraps them on first render. */
  body: string;
  inReplyTo?: string;
  references?: string[];
  /** Server-side assets to fetch and attach when the composer mounts.
   *  Used by the chat photo handoff and the documents "send via email"
   *  button. Composer fetches each URL with credentials, builds a File
   *  from the blob, and feeds it through the existing addFiles() path
   *  so it counts toward the 25 MB cap exactly like a manual attach. */
  pendingAttachments?: Array<{
    url:      string;
    filename: string;
    mimetype?: string;
  }>;
}

interface Props {
  accounts: EmailAccount[];
  initial: ComposeDraft;
  onClose: () => void;
  onSent: () => void;
}

// Autosave bucket — replies are keyed by inReplyTo so the user can
// have multiple drafts in flight without overwriting each other.
const AUTOSAVE_KEY = "yorik_email_compose_draft";
const MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024;

interface ContactSuggestion {
  id: number;
  display_name: string;
  email: string;
}

interface AttachmentDraft {
  filename: string;
  mimetype: string;
  size: number;
  content_b64: string;
  /** Server URL the file was fetched from on mount (chat photo
   *  handoff, documents "Send via email" button). Empty for files
   *  the user dragged in from disk. Sent to /compose/drafts so the
   *  LLM-side draft pipeline can pull a real content snippet for
   *  PDFs / docs instead of inventing context. */
  source_url?: string;
}

/** Pure helper — convert plain text into a sequence of <p> paragraphs
 *  so TipTap renders it cleanly. Reply prefills come in as plain
 *  text with newline-separated quote markers; we preserve them as-is
 *  inside paragraphs (no auto-blockquote in v1). */
function textToInitialHtml(s: string): string {
  if (!s) return "";
  // Detect HTML — if the prefill already contains a tag, trust it.
  if (/<[a-z][\s\S]*>/i.test(s)) return s;
  const escape = (t: string) => t
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return s.split(/\n{2,}/)
    .map(p => `<p>${escape(p).replace(/\n/g, "<br/>")}</p>`)
    .join("");
}

/** Strip tags into plain text for the body_text fallback — kept
 *  simple. Replaces <br> with \n, paragraph breaks with double \n. */
function htmlToPlainText(html: string): string {
  if (!html) return "";
  const doc = new DOMParser().parseFromString(html, "text/html");
  return (doc.body.textContent || "").trim();
}

/** Read a single File as base64 (without the data URI prefix). */
function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("FileReader returned non-string"));
        return;
      }
      const i = result.indexOf(",");
      resolve(i >= 0 ? result.slice(i + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

export function Composer({ accounts, initial, onClose, onSent }: Props) {
  const defaultAccount = accounts.find(a => a.is_default) || accounts[0];

  // Restore an autosaved draft when opening a NEW compose (no inReplyTo).
  // Replies always use the freshly-prefilled `initial` — we don't want
  // an old draft for a different thread leaking in.
  const restored = useMemo(() => {
    if (initial.inReplyTo) return null;
    try {
      const raw = localStorage.getItem(AUTOSAVE_KEY);
      if (!raw) return null;
      const d = JSON.parse(raw) as ComposeDraft & { bodyHtml?: string };
      if ((d.to || d.subject || d.body || d.bodyHtml || d.cc || "").trim()) return d;
      return null;
    } catch { return null; }
  }, [initial.inReplyTo]);

  const [accountId, setAccountId] = useState<number>(
    restored?.accountId || initial.accountId || defaultAccount?.id || 0
  );
  const [to, setTo] = useState(restored?.to ?? initial.to);
  const [cc, setCc] = useState(restored?.cc ?? initial.cc ?? "");
  const [showCc, setShowCc] = useState(!!(restored?.cc ?? initial.cc));
  const [subject, setSubject] = useState(restored?.subject ?? initial.subject);
  const [minimized, setMinimized] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [restoredHint, setRestoredHint] = useState<boolean>(!!restored);
  const [attachments, setAttachments] = useState<AttachmentDraft[]>([]);
  const [dragOver, setDragOver] = useState(false);

  // TipTap editor for the body. Initial content comes from restored
  // draft → reply prefill → blank. We persist HTML in autosave.
  const initialHtml = useMemo(() => {
    if (restored?.body) return restored.body;
    return textToInitialHtml(initial.body || "");
  }, [restored, initial.body]);

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        // StarterKit 3 bundles link + underline; disable so our
        // standalones don't collide with duplicate-extension warnings.
        link: false,
        underline: false,
        heading: false,  // no H1/H2/H3 in email — keeps it inbox-clean
      }),
      Underline,
      Link.configure({ openOnClick: false, autolink: true }),
      Placeholder.configure({ placeholder: "Write your message…" }),
    ],
    content: initialHtml,
    editorProps: {
      attributes: {
        class: "email-compose-prose focus:outline-none min-h-[200px] px-3 py-3 text-sm",
      },
      handleDrop(_view, event) {
        // Defer file drops to the wrapper's onDrop handler — TipTap
        // would otherwise insert the dropped file's metadata as text.
        if (event.dataTransfer?.files?.length) {
          // Return true tells ProseMirror "handled" so it doesn't
          // try to interpret the drop. The wrapper handler still
          // fires (event still bubbles).
          return true;
        }
        return false;
      },
    },
  });

  // Autosave every 3s of inactivity, BUT only for the new-compose
  // path (reply drafts would clobber each other on this single key).
  // We store HTML in `body` so the restore round-trips cleanly.
  useEffect(() => {
    if (initial.inReplyTo) return;
    if (!editor) return;
    const handle = window.setTimeout(() => {
      try {
        localStorage.setItem(AUTOSAVE_KEY, JSON.stringify({
          accountId, to, cc, subject,
          body: editor.getHTML(),
        }));
      } catch {}
    }, 3000);
    return () => window.clearTimeout(handle);
  }, [accountId, to, cc, subject, editor, initial.inReplyTo]);

  async function addFiles(
    files: FileList | File[],
    sources?: ReadonlyArray<string | undefined>,
  ) {
    setError(null);
    const cur = attachments.reduce((acc, a) => acc + a.size, 0);
    const next: AttachmentDraft[] = [...attachments];
    let total = cur;
    const arr = Array.from(files);
    for (let i = 0; i < arr.length; i++) {
      const f = arr[i];
      if (total + f.size > MAX_TOTAL_ATTACHMENT_BYTES) {
        setError(`Attachments exceed 25 MB total (${f.name} pushed past the cap).`);
        break;
      }
      try {
        const b64 = await fileToBase64(f);
        next.push({
          filename: f.name,
          mimetype: f.type || "application/octet-stream",
          size:     f.size,
          content_b64: b64,
          source_url: sources?.[i],
        });
        total += f.size;
      } catch (err: any) {
        setError(`Couldn't attach ${f.name}: ${err?.message || err}`);
      }
    }
    setAttachments(next);
  }

  function removeAttachment(idx: number) {
    setAttachments(prev => prev.filter((_, i) => i !== idx));
  }

  // Programmatic attach from server-side URLs. Used by the chat photo
  // handoff and the documents → "send via email" button so the user
  // doesn't have to download-then-re-attach. Fetches with credentials
  // (same-origin cookie auth), builds a File from the blob, then
  // feeds it through addFiles so the 25 MB cap + dedupe + error
  // surface are all unified. Runs exactly once on mount; resetting
  // the ref guarantees a parent re-render of <Composer> with a new
  // pendingAttachments value won't double-attach.
  const pendingFetchedRef = useRef(false);
  useEffect(() => {
    if (pendingFetchedRef.current) return;
    const pending = initial.pendingAttachments;
    if (!pending?.length) return;
    pendingFetchedRef.current = true;
    (async () => {
      const files: File[] = [];
      const sources: string[] = [];
      for (const p of pending) {
        try {
          const r = await fetch(p.url, { credentials: "include" });
          if (!r.ok) {
            // Surface FastAPI's `detail` so the user sees the *reason*
            // ("Immich not configured", "invalid asset_id", role error)
            // instead of just the status code.
            let why = `HTTP ${r.status}`;
            try {
              const j = await r.json();
              const d = j?.detail;
              if (typeof d === "string") why += ` — ${d}`;
              else if (d) why += ` — ${JSON.stringify(d)}`;
            } catch {}
            setError(`Couldn't fetch ${p.filename}: ${why}`);
            continue;
          }
          const blob = await r.blob();
          const type = p.mimetype || blob.type || "application/octet-stream";
          files.push(new File([blob], p.filename, { type }));
          // Keep the source URL paired by index so addFiles can stamp
          // it onto the AttachmentDraft. Lets /compose/drafts pull a
          // text snippet from PDFs / docs server-side.
          sources.push(p.url);
        } catch (err: any) {
          const msg = typeof err === "string"
            ? err
            : (err?.message || err?.toString?.() || JSON.stringify(err));
          setError(`Couldn't fetch ${p.filename}: ${msg}`);
        }
      }
      if (files.length) await addFiles(files, sources);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleSend() {
    setError(null);
    if (!editor) return;
    setSending(true);
    try {
      const splitAddrs = (s: string) => s.split(/[,;]/).map(x => x.trim()).filter(Boolean);
      const html = editor.getHTML();
      const text = htmlToPlainText(html);
      await api.post("/api/email/send", {
        account_id: accountId,
        to: splitAddrs(to),
        cc: showCc ? splitAddrs(cc) : [],
        subject,
        body_text: text,
        body_html: html,
        in_reply_to: initial.inReplyTo,
        references: initial.references || [],
        attachments: attachments.map(a => ({
          filename:    a.filename,
          mimetype:    a.mimetype,
          content_b64: a.content_b64,
        })),
      });
      // Clear the autosave so the next blank compose doesn't restore.
      try { localStorage.removeItem(AUTOSAVE_KEY); } catch {}
      onSent();
      onClose();
    } catch (e: any) {
      setError(e.message || "send failed");
    } finally {
      setSending(false);
    }
  }

  function discardDraft() {
    try { localStorage.removeItem(AUTOSAVE_KEY); } catch {}
    onClose();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      void handleSend();
    }
  }

  if (minimized) {
    return (
      <button
        onClick={() => setMinimized(false)}
        className="fixed bottom-4 right-4 z-40 bg-card border border-border rounded-t-md px-4 py-2 text-sm font-medium shadow-lg flex items-center gap-2 hover:bg-muted"
      >
        <Send className="w-3.5 h-3.5" />
        {subject || "(no subject)"} ↑
      </button>
    );
  }

  return (
    <div
      onKeyDown={onKeyDown}
      onDragOver={(e) => {
        if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
          e.preventDefault();
          setDragOver(true);
        }
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        if (!e.dataTransfer?.files?.length) return;
        e.preventDefault();
        setDragOver(false);
        void addFiles(e.dataTransfer.files);
      }}
      className={cn(
        "fixed z-40 bg-card border flex flex-col transition",
        // Mobile: full-screen — small floating cards don't survive
        // the keyboard opening or thumb-typing in cramped fields.
        "inset-0 w-full h-full rounded-none pb-[env(safe-area-inset-bottom)]",
        // Desktop: original floating card bottom-right.
        "md:inset-auto md:bottom-4 md:right-4 md:w-[640px] md:max-w-[calc(100vw-2rem)] md:max-h-[85vh] md:rounded-lg md:shadow-2xl md:pb-0",
        dragOver ? "border-amber-500/60 ring-2 ring-amber-500/30" : "border-border",
      )}
    >
      <div className="flex items-center justify-between px-4 py-2 bg-secondary/40 md:rounded-t-lg border-b border-border">
        <span className="text-sm font-medium truncate">{subject || "New message"}</span>
        <div className="flex gap-1">
          {/* Minimize hidden on mobile — full-screen composer can't be
              minimised to a corner, the corner doesn't exist on mobile. */}
          <button onClick={() => setMinimized(true)} className="hidden md:inline-flex p-1 hover:bg-muted rounded" title="Minimize">
            <Minus className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={onClose}
            className="p-2.5 md:p-1 hover:bg-muted rounded min-w-[40px] min-h-[40px] md:min-w-0 md:min-h-0 inline-flex items-center justify-center"
            title="Close (draft kept)"
            aria-label="Close"
          >
            <X className="w-5 h-5 md:w-3.5 md:h-3.5" />
          </button>
        </div>
      </div>

      {restoredHint && (
        <div className="px-3 py-1.5 text-[11px] bg-emerald-500/[0.06] border-b border-emerald-500/20 text-emerald-700 dark:text-emerald-400 flex items-center justify-between">
          <span>Restored your last draft.</span>
          <button
            onClick={() => { discardDraft(); }}
            className="text-emerald-700/80 dark:text-emerald-400/80 hover:underline"
            title="Throw away the restored draft and close"
          >
            discard
          </button>
        </div>
      )}

      <div className="p-3 space-y-2 border-b border-border text-sm">
        {accounts.length > 1 && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground w-12">From</span>
            <select
              value={accountId}
              onChange={e => setAccountId(+e.target.value)}
              className="flex-1 h-7 px-2 rounded bg-muted text-sm focus:outline-none"
            >
              {accounts.map(a => (
                <option key={a.id} value={a.id}>
                  {a.display_name || a.email} ({a.email})
                </option>
              ))}
            </select>
          </div>
        )}
        <RecipientField label="To" value={to} onChange={setTo} />
        {!showCc && (
          <div className="text-right">
            <button onClick={() => setShowCc(true)} className="text-xs text-muted-foreground hover:text-foreground">
              + Cc
            </button>
          </div>
        )}
        {showCc && (
          <RecipientField label="Cc" value={cc} onChange={setCc} />
        )}
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground w-12">Subject</span>
          <input
            value={subject}
            onChange={e => setSubject(e.target.value)}
            placeholder="Subject"
            className="flex-1 h-7 px-2 bg-transparent text-sm focus:outline-none font-medium"
          />
        </div>
      </div>

      {/* AI draft panel — only on NEW compose. Reply mode already has
          the inbox-side AIDraftPanel, so showing this one too would
          duplicate the affordance. */}
      {!initial.inReplyTo && editor && (
        <NewEmailDraftPanel
          to={to}
          subject={subject}
          attachments={attachments.map(a => ({
            filename:   a.filename,
            mimetype:   a.mimetype,
            source_url: a.source_url,
          }))}
          onPick={(text) => {
            editor.commands.setContent(textToInitialHtml(text));
            editor.commands.focus();
            setRestoredHint(false);
          }}
          onSuggestSubject={(s) => {
            // Only fill when the user hasn't typed anything — never
            // clobber their own input.
            if (s && !subject.trim()) setSubject(s);
          }}
        />
      )}

      {/* Editor + toolbar — flex column so the editor itself can scroll
          inside the modal's max-height without overflowing the chrome. */}
      <div className="flex-1 min-h-0 flex flex-col">
        <EditorToolbar editor={editor} onAttachClick={() => {
          // Trigger the hidden file input below.
          (document.getElementById("yorik-email-attach-input") as HTMLInputElement)?.click();
        }} />
        <div className="flex-1 min-h-0 overflow-y-auto" onClick={() => { editor?.commands.focus(); setRestoredHint(false); }}>
          {editor && <EditorContent editor={editor} />}
        </div>
      </div>

      {attachments.length > 0 && (
        <div className="px-3 py-2 border-t border-border bg-muted/20 flex flex-wrap gap-1.5">
          {attachments.map((a, i) => (
            <AttachmentChip key={i} att={a} onRemove={() => removeAttachment(i)} />
          ))}
        </div>
      )}

      {dragOver && (
        <div className="px-3 py-1.5 text-[11px] text-amber-700 dark:text-amber-400 text-center border-t border-amber-500/20 bg-amber-500/[0.05]">
          Drop to attach
        </div>
      )}

      {error && (
        <div className="px-3 py-2 mx-3 mb-2 flex gap-2 bg-destructive/10 border border-destructive/30 rounded-md text-xs">
          <AlertCircle className="w-3.5 h-3.5 text-destructive shrink-0 mt-0.5" />
          <span className="text-destructive">{error}</span>
        </div>
      )}

      <div className="p-3 border-t border-border flex items-center gap-2">
        <button
          onClick={handleSend}
          disabled={!to.trim() || sending || !accountId}
          className="px-4 h-9 text-sm rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-2"
        >
          {sending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
          Send
        </button>
        <button
          onClick={() => (document.getElementById("yorik-email-attach-input") as HTMLInputElement)?.click()}
          className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted transition"
          title="Attach file"
        >
          <Paperclip className="w-4 h-4" />
        </button>
        <input
          id="yorik-email-attach-input"
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            if (e.target.files) void addFiles(e.target.files);
            e.target.value = "";  // allow re-picking the same file
          }}
        />
        <span className="text-xs text-muted-foreground ml-auto">
          {sending ? "sending…" : "⌘/Ctrl+Enter to send · drop files anywhere"}
        </span>
      </div>

      {/* Local styles for the TipTap-rendered body. Keeps the email
          composer feeling tighter than the long-form Compose route. */}
      <style>{`
        .email-compose-prose p { margin: 0 0 0.5em 0; }
        .email-compose-prose p:last-child { margin-bottom: 0; }
        .email-compose-prose ul, .email-compose-prose ol { margin: 0.25em 0 0.5em 1.5em; padding: 0; }
        .email-compose-prose li { margin: 0.1em 0; }
        .email-compose-prose blockquote {
          margin: 0.5em 0;
          padding-left: 0.75em;
          border-left: 3px solid hsl(var(--border));
          color: hsl(var(--muted-foreground));
        }
        .email-compose-prose a { color: hsl(var(--primary)); text-decoration: underline; }
        .email-compose-prose p.is-editor-empty:first-child::before {
          content: attr(data-placeholder);
          float: left;
          color: hsl(var(--muted-foreground));
          pointer-events: none;
          height: 0;
        }
      `}</style>
    </div>
  );
}


// ─── editor toolbar ─────────────────────────────────────────────
function EditorToolbar({ editor, onAttachClick }: {
  editor: Editor | null;
  onAttachClick: () => void;
}) {
  if (!editor) return <div className="h-9 border-b border-border" />;
  return (
    <div className="px-2 py-1 border-b border-border flex items-center gap-0.5">
      <TBtn active={editor.isActive("bold")}
            onClick={() => editor.chain().focus().toggleBold().run()}
            title="Bold"><BoldIcon className="w-3.5 h-3.5" /></TBtn>
      <TBtn active={editor.isActive("italic")}
            onClick={() => editor.chain().focus().toggleItalic().run()}
            title="Italic"><ItalicIcon className="w-3.5 h-3.5" /></TBtn>
      <TBtn active={editor.isActive("underline")}
            onClick={() => editor.chain().focus().toggleUnderline().run()}
            title="Underline"><UnderlineIcon className="w-3.5 h-3.5" /></TBtn>
      <div className="w-px h-4 bg-border mx-1" />
      <TBtn active={editor.isActive("bulletList")}
            onClick={() => editor.chain().focus().toggleBulletList().run()}
            title="Bulleted list"><ListIcon className="w-3.5 h-3.5" /></TBtn>
      <TBtn active={editor.isActive("orderedList")}
            onClick={() => editor.chain().focus().toggleOrderedList().run()}
            title="Numbered list"><ListOrdered className="w-3.5 h-3.5" /></TBtn>
      <TBtn active={editor.isActive("blockquote")}
            onClick={() => editor.chain().focus().toggleBlockquote().run()}
            title="Quote"><Quote className="w-3.5 h-3.5" /></TBtn>
      <div className="w-px h-4 bg-border mx-1" />
      <TBtn active={editor.isActive("link")}
            onClick={() => {
              const prev = editor.getAttributes("link").href as string | undefined;
              const url = window.prompt("Link URL", prev || "https://");
              if (url === null) return;
              if (url === "") {
                editor.chain().focus().extendMarkRange("link").unsetLink().run();
              } else {
                editor.chain().focus().extendMarkRange("link").setLink({ href: url }).run();
              }
            }}
            title="Link"><LinkIcon className="w-3.5 h-3.5" /></TBtn>
      <div className="flex-1" />
      <TBtn onClick={onAttachClick} title="Attach file">
        <Paperclip className="w-3.5 h-3.5" />
      </TBtn>
    </div>
  );
}

function TBtn({ active, onClick, title, children }: {
  active?: boolean;
  onClick: () => void;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={cn(
        "p-1.5 rounded transition",
        active
          ? "bg-violet-500/15 text-violet-600 dark:text-violet-400"
          : "text-muted-foreground hover:text-foreground hover:bg-muted",
      )}
    >
      {children}
    </button>
  );
}


// ─── attachment chip ────────────────────────────────────────────
function AttachmentChip({ att, onRemove }: {
  att: AttachmentDraft;
  onRemove: () => void;
}) {
  return (
    <div
      className="inline-flex items-center gap-2 max-w-[220px] pl-2 pr-1 py-1 rounded-md border border-border bg-card text-xs"
      title={att.filename}
    >
      <Paperclip className="w-3 h-3 text-muted-foreground shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="truncate font-medium">{att.filename}</div>
        <div className="text-[10px] text-muted-foreground">{humanSize(att.size)}</div>
      </div>
      <button
        type="button"
        onClick={onRemove}
        className="p-0.5 rounded text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10"
        title="Remove"
      >
        <X className="w-3 h-3" />
      </button>
    </div>
  );
}

function humanSize(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "?";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}


// ─── recipient field with contact autocomplete ──────────────────
//
// On every keystroke we look at the LAST comma-separated token —
// that's what the user is typing. We call /api/chat/mentions (which
// the chat composer also uses) for contacts matching that prefix,
// then render a small dropdown below the input. Pick → splice the
// chosen email back into the field in place of the partial token.

function RecipientField({ label, value, onChange }: {
  label: string;
  value: string;
  onChange: (next: string) => void;
}) {
  const [suggestions, setSuggestions] = useState<ContactSuggestion[]>([]);
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const reqIdRef = useRef(0);

  // Pull the partial token under the caret (the one after the last
  // comma/semicolon). When it's < 2 chars, hide the popover instead
  // of spamming /api with empty queries.
  const partial = useMemo(() => {
    const m = value.match(/[^,;]*$/);
    return (m ? m[0] : "").trim();
  }, [value]);

  // Debounced fetch — 120ms.
  useEffect(() => {
    if (partial.length < 2) { setSuggestions([]); setOpen(false); return; }
    const id = ++reqIdRef.current;
    const handle = window.setTimeout(async () => {
      try {
        const r = await api.get<{
          contact: Array<{ id: number; label: string; sub?: string }>;
        }>(`/api/chat/mentions?prefix=${encodeURIComponent(partial)}&types=contact&limit=6`);
        if (id !== reqIdRef.current) return;
        const detailed = await Promise.all(r.contact.map(async (c) => {
          try {
            const d = await api.get<{
              display_name: string;
              channels: Array<{ kind: string; value: string }>;
            }>(`/api/contacts/${c.id}`);
            const em = d.channels.find(ch => ch.kind === "email");
            if (!em) return null;
            return {
              id: c.id, display_name: d.display_name, email: em.value,
            } as ContactSuggestion;
          } catch { return null; }
        }));
        const filtered = detailed.filter(Boolean) as ContactSuggestion[];
        setSuggestions(filtered);
        setHighlight(0);
        setOpen(filtered.length > 0);
      } catch {
        setSuggestions([]);
        setOpen(false);
      }
    }, 120);
    return () => window.clearTimeout(handle);
  }, [partial]);

  function applyPick(s: ContactSuggestion) {
    const idx = value.lastIndexOf(partial);
    const head = idx >= 0 ? value.slice(0, idx) : value + " ";
    const insertion = s.display_name ? `${s.display_name} <${s.email}>` : s.email;
    const next = head + insertion + ", ";
    onChange(next);
    setOpen(false);
    setSuggestions([]);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open || suggestions.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight(h => (h + 1) % suggestions.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight(h => (h - 1 + suggestions.length) % suggestions.length);
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      applyPick(suggestions[highlight]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="relative">
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground w-12">{label}</span>
        <input
          ref={inputRef}
          value={value}
          onChange={e => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="recipient@…"
          className="flex-1 h-7 px-2 bg-transparent text-sm focus:outline-none"
        />
      </div>
      {open && suggestions.length > 0 && (
        <div className="absolute left-14 right-0 top-full mt-1 z-50 max-w-md rounded-md border border-border bg-popover shadow-lg overflow-hidden">
          <div className="px-3 py-1 text-[9px] uppercase tracking-wider text-muted-foreground font-semibold flex items-center gap-1 border-b border-border bg-muted/30">
            <UsersRound className="w-2.5 h-2.5" /> Contacts matching "{partial}"
          </div>
          {suggestions.map((s, i) => (
            <button
              key={s.id}
              type="button"
              onMouseEnter={() => setHighlight(i)}
              onClick={() => applyPick(s)}
              className={cn(
                "w-full text-left px-3 py-1.5 text-xs flex items-center gap-2 transition",
                i === highlight ? "bg-violet-500/10" : "hover:bg-muted/50",
              )}
            >
              <div className="flex-1 min-w-0">
                <div className="font-medium truncate">{s.display_name}</div>
                <div className="text-[10px] text-muted-foreground truncate">{s.email}</div>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}


/**
 * AI draft panel for NEW emails (no thread to reply to).
 *
 * Calls /api/email/compose/drafts with {intent, to, subject, state}
 * and renders 3 cards. Clicking a card replaces the editor body.
 * Mirrors AIDraftPanel in EmailApp so the new-compose and reply
 * paths feel like the same feature from the user's side.
 */
interface NewEmailDraftPanelProps {
  to:      string;
  subject: string;
  /** Files already added to the message, so the LLM can reference
   *  them naturally in the body ("the attached price list"). Only
   *  filename + mimetype + source URL are sent — no payload. The
   *  URL lets the backend pull a real content snippet for PDFs /
   *  docs so the LLM has actual grounding instead of inventing. */
  attachments?: Array<{
    filename:    string;
    mimetype?:   string;
    source_url?: string;
  }>;
  onPick:  (text: string) => void;
  /** Called when the backend returns a suggested subject line. The
   *  parent decides whether to apply it (typically: only when the
   *  user hasn't typed their own subject). */
  onSuggestSubject?: (subject: string) => void;
}

export function NewEmailDraftPanel({
  to, subject, attachments, onPick, onSuggestSubject,
}: NewEmailDraftPanelProps) {
  const statesApi = useApi<Array<{
    key: string; label_en: string; label_de: string; tone: string;
  }>>("/api/email/draft-states", []);
  const states = Array.isArray(statesApi.data) ? statesApi.data : [];

  const [intent, setIntent] = useState("");
  const [activeState, setActiveState] = useState<string | null>(null);
  const [variants, setVariants] = useState<Array<{ id: number; label: string; text: string }>>([]);
  const [generating, setGenerating] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Collapsed by default for users who just want to write — clicking
  // the header expands the panel.
  const [open, setOpen] = useState(false);

  async function generate(overrideState?: string | null) {
    if (!intent.trim()) {
      setErr("Type a quick intent first — what should Yorik write?");
      return;
    }
    const effective = overrideState === undefined ? activeState : overrideState;
    if (overrideState !== undefined) setActiveState(overrideState);
    setGenerating(true);
    setErr(null);
    try {
      const r = await api.post<{
        variants: Array<{ id: number; label: string; text: string }>;
        suggested_subject?: string;
      }>("/api/email/compose/drafts", {
        intent: intent.trim(),
        to:      to || undefined,
        subject: subject || undefined,
        state:   effective || undefined,
        attachments: (attachments || []).map(a => ({
          filename:   a.filename,
          mimetype:   a.mimetype || null,
          source_url: a.source_url || null,
        })),
      });
      setVariants(r.variants || []);
      if (r.suggested_subject && onSuggestSubject) {
        onSuggestSubject(r.suggested_subject);
      }
    } catch (e: any) {
      const msg = typeof e === "string"
        ? e
        : (e?.message || e?.toString?.() || JSON.stringify(e));
      setErr(`Couldn't generate drafts: ${msg}`);
    } finally {
      setGenerating(false);
    }
  }

  return (
    <div className="border-y border-border bg-violet-500/[0.04]">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full px-3 py-2 flex items-center gap-2 text-xs font-medium text-foreground/80 hover:bg-violet-500/[0.06] transition"
      >
        <Sparkles className="w-3.5 h-3.5 text-violet-500" />
        <span className="uppercase tracking-wider">AI drafts</span>
        <span className="text-muted-foreground font-normal normal-case tracking-normal">
          {open
            ? "— pick a tone, describe the intent, get 3 suggestions"
            : "click to expand"}
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3">
          {/* Tone chips. Clicking one immediately regenerates with that
              tone if we already have an intent — otherwise it just
              records the choice for the next Generate. */}
          <div className="flex flex-wrap gap-1.5">
            {states.map(s => {
              const isActive = activeState === s.key;
              const tint = TONE_TINTS[s.key] || TONE_TINT_DEFAULT;
              return (
                <button
                  key={s.key}
                  type="button"
                  onClick={() => {
                    const next = isActive ? null : s.key;
                    if (intent.trim()) generate(next);
                    else setActiveState(next);
                  }}
                  disabled={generating}
                  title={s.tone}
                  className={cn(
                    "text-[11px] px-2.5 py-1 rounded-full transition disabled:opacity-50",
                    isActive ? tint.active : tint.idle,
                  )}
                >
                  {s.label_en || s.label_de}
                </button>
              );
            })}
          </div>

          <textarea
            value={intent}
            onChange={e => setIntent(e.target.value)}
            placeholder='What should it say? e.g. "Politely follow up on whether the invoice has been paid."'
            className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-violet-500/30 min-h-[60px] resize-y"
            disabled={generating}
          />

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => generate()}
              disabled={generating || !intent.trim()}
              className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-violet-500 hover:bg-violet-600 text-white shadow-sm transition disabled:opacity-50"
            >
              {generating
                ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Generating…</>
                : <><Sparkles className="w-3.5 h-3.5" /> {variants.length ? "Regenerate" : "Generate 3 drafts"}</>}
            </button>
            {variants.length > 0 && (
              <button
                type="button"
                onClick={() => { setVariants([]); setErr(null); }}
                className="text-[11px] text-muted-foreground hover:text-foreground transition px-1.5 py-1"
              >
                Discard
              </button>
            )}
          </div>

          {err && (
            <div className="text-[11px] text-rose-600 dark:text-rose-400 flex items-center gap-1">
              <AlertCircle className="w-3 h-3" /> {err}
            </div>
          )}

          {variants.length > 0 && (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
              {variants.map(v => (
                <button
                  key={v.id}
                  type="button"
                  onClick={() => { onPick(v.text); setOpen(false); }}
                  className="text-left p-3 rounded-md bg-card border border-border hover:border-violet-500/60 hover:bg-accent transition group"
                >
                  <div className="text-[10px] uppercase tracking-wider font-semibold text-violet-500 mb-1.5">
                    {v.label}
                  </div>
                  <div className="text-xs text-foreground/90 line-clamp-6 group-hover:text-foreground whitespace-pre-line">
                    {v.text}
                  </div>
                  <div className="text-[10px] text-muted-foreground mt-2 italic">
                    Click to use this draft
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
