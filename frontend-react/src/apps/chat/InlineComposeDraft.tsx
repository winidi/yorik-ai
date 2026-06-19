/**
 * Inline Compose draft card — rendered in chat when the LLM emits a
 * `compose_draft_created` action. This is the v1 "magical" Compose
 * experience: the user reads, edits, iterates, and sends the draft
 * right inside the chat thread without ever opening the Compose app.
 *
 * Four built-in capabilities:
 *
 *   1. Full TipTap editor inline. Edits auto-save (1s debounce) via
 *      PATCH /api/compose/saved-draft/{id}. Open the Compose app any
 *      time for the bigger toolbar / table editing / inline photos.
 *
 *   2. "Refine via chat" input below the editor. User types
 *      "make it shorter" / "use Sie form" / "remove the Friday part" —
 *      hits enter — backend LLM rewrites the body preserving HTML,
 *      editor swaps to the new content. 3-8s on a local LLM.
 *
 *   3. Recipient picker. If the draft has no recipient, or the user
 *      wants to send to someone different, the inline picker searches
 *      contacts and binds a contact_id for the send call. Shows the
 *      contact's available channels (email / whatsapp) so the user
 *      can see what send methods are reachable.
 *
 *   4. Inline send. Three methods exposed as a radio:
 *        ▸ Email     — uses the user's default email account
 *        ▸ WhatsApp  — body text (HTML stripped); short-message check
 *        ▸ PDF       — renders + opens; user prints/posts themselves
 *      LLM-pre-selected based on the draft's `kind` (invoice → email,
 *      casual letter → whatsapp, formal letter → pdf), user can swap.
 *      "Send" disables during the call; success shows a green confirm.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { EditorContent, useEditor } from "@tiptap/react";
import type { Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Underline } from "@tiptap/extension-underline";
import { Link } from "@tiptap/extension-link";
import { Placeholder } from "@tiptap/extension-placeholder";
import { Image } from "@tiptap/extension-image";
import {
  Send, Wand2, Mail, MessageCircle, FileText, Check, Loader2,
  Sparkles, ChevronDown, X, ExternalLink, Pencil, AlertCircle,
  History, RotateCcw,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

// ─── Types ───────────────────────────────────────────────────────────

interface DraftFull {
  id:          number;
  kind:        string;
  template_id: string | null;
  recipient:   string | null;
  subject:     string | null;
  body_html:   string;
  args:        Record<string, unknown>;
  updated_at:  string;
}

interface DraftVersion {
  id:            number;
  source:        "initial" | "refine" | "manual" | "restore";
  instruction:   string | null;
  restored_from: number | null;
  created_at:    string;
}

interface ContactHit {
  id:           number;
  display_name: string;
  kind:         "person" | "business";
  channels:     Array<{ id: number; kind: string; value: string }>;
}

type SendMethod = "email" | "whatsapp" | "pdf";

interface SendResult {
  ok:      boolean;
  method?: string;
  to?:     string;
  pdf_url?: string;
}

// ─── Helpers ─────────────────────────────────────────────────────────

const KIND_LABEL: Record<string, string> = {
  letter:  "Brief",
  invoice: "Rechnung",
  offer:   "Angebot",
  email:   "E-Mail",
  memo:    "Notiz",
};

function kindIcon(kind: string): string {
  if (kind === "invoice" || kind === "offer") return "💶";
  if (kind === "email") return "📧";
  return "📄";
}

/** Default send method based on the draft's kind. Invoices + formal
 *  letters lean PDF; emails kind obviously lean email; everything else
 *  defaults to email. The user can swap. */
function defaultMethodFor(kind: string): SendMethod {
  if (kind === "invoice" || kind === "offer") return "pdf";
  if (kind === "email") return "email";
  return "email";
}

// ─── Component ───────────────────────────────────────────────────────

interface Props {
  draftId:      number;
  // Optional initial-render hints from the chat action (faster paint
  // before the full draft fetch completes).
  kind?:        string;
  recipient?:   string;
  subject?:     string;
  preview?:     string;
  templateId?:  string | null;
  templateName?: string | null;
  missingArgs?: string[];
}

export function InlineComposeDraft({
  draftId, kind: initialKind, recipient: initialRecipient,
  subject: initialSubject, preview, templateId, templateName, missingArgs,
}: Props) {
  const navigate = useNavigate();

  // ── Draft state ─────────────────────────────────────────────────
  const [draft, setDraft] = useState<DraftFull | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [versions, setVersions] = useState<DraftVersion[]>([]);
  // The version chip the user has highlighted. Defaults to the last
  // one (current draft body). Picking a previous chip triggers a
  // restore — backend swaps body_html + appends a 'restore' row, and
  // we pick up the new "current" via the refetch.
  const [highlightedVersionId, setHighlightedVersionId] = useState<number | null>(null);

  const loadDraft = useCallback(async () => {
    try {
      const d = await api.get<DraftFull>(`/api/compose/saved-draft/${draftId}`);
      setDraft(d);
      setLoadError(null);
    } catch (e: any) {
      setLoadError(e?.message || "Couldn't load draft");
    }
  }, [draftId]);

  const loadVersions = useCallback(async () => {
    try {
      const vs = await api.get<DraftVersion[]>(`/api/compose/saved-draft/${draftId}/versions`);
      setVersions(vs || []);
      if (vs?.length) setHighlightedVersionId(vs[vs.length - 1].id);
    } catch {
      // version history is non-critical — silent on failure
    }
  }, [draftId]);

  useEffect(() => { loadDraft(); loadVersions(); }, [loadDraft, loadVersions]);

  async function restoreVersion(versionId: number) {
    try {
      const r = await api.post<{ new_version_id: number; body_html: string }>(
        `/api/compose/saved-draft/${draftId}/restore`,
        { version_id: versionId },
      );
      if (editor) editor.commands.setContent(r.body_html);
      setDraft(d => d ? { ...d, body_html: r.body_html } : d);
      await loadVersions();
      setHighlightedVersionId(r.new_version_id);
    } catch (e: any) {
      setRefineError(`Restore failed: ${e?.message || e}`);
    }
  }

  // ── TipTap editor ───────────────────────────────────────────────
  const editor = useEditor({
    extensions: [
      StarterKit.configure({ link: false, underline: false }),
      Underline,
      Link.configure({ openOnClick: false }),
      Placeholder.configure({ placeholder: "Empty draft — start typing or use Refine below…" }),
      // allowBase64=true mirrors ComposeApp.tsx: data: URLs land in
      // body_html via compose_draft's inline_image_url embed pass
      // (signature_data_url + find_photo thumbnails). Without this
      // extension the chat card stripped every <img> on render —
      // user saw "Liebesbrief mit Foto" turn into a Foto-less letter
      // inside the chat thread while the Compose-app version still
      // had the photo.
      Image.configure({
        allowBase64: true,
        HTMLAttributes: { class: "compose-image max-w-full h-auto rounded" },
      }),
    ],
    content: "",
    editorProps: {
      attributes: {
        class: "prose prose-sm dark:prose-invert max-w-none focus:outline-none px-3 py-2.5 min-h-[120px]",
      },
    },
  });

  // Sync editor content when the draft first loads. We compare against
  // current editor HTML to avoid clobbering live user edits when the
  // refine endpoint round-trips (which also updates draft.body_html).
  useEffect(() => {
    if (!editor || !draft) return;
    if (editor.getHTML() !== draft.body_html) {
      editor.commands.setContent(draft.body_html || "<p></p>");
    }
  }, [editor, draft?.body_html]);

  // Debounced auto-save on edit. 1s after the last keystroke we PATCH
  // the draft so refreshes / returning later get the latest content.
  const saveTimerRef = useRef<number | null>(null);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved">("idle");
  useEffect(() => {
    if (!editor || !draft) return;
    const handler = () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      setSaveState("idle");
      saveTimerRef.current = window.setTimeout(async () => {
        setSaveState("saving");
        const html = editor.getHTML();
        try {
          await api.patch(`/api/compose/saved-draft/${draftId}`, {
            kind:        draft.kind,
            template_id: draft.template_id,
            recipient:   draft.recipient,
            subject:     draft.subject,
            body_html:   html,
            args:        draft.args,
          });
          setSaveState("saved");
          // Update local draft so the saved badge sticks until next edit
          setDraft(d => d ? { ...d, body_html: html } : d);
          setTimeout(() => setSaveState("idle"), 2000);
        } catch {
          setSaveState("idle");
        }
      }, 1000);
    };
    editor.on("update", handler);
    return () => { editor.off("update", handler); };
  }, [editor, draft, draftId]);

  // ── Refine via chat (streaming) ─────────────────────────────────
  const [refineInput, setRefineInput] = useState("");
  const [refining, setRefining] = useState(false);
  const [refineError, setRefineError] = useState<string | null>(null);
  // Live streaming text appended token-by-token during the refine
  // call. We render it as a translucent overlay above the editor so
  // the user sees the LLM "thinking" without us having to incrementally
  // re-render the TipTap editor on every chunk (which would flicker /
  // break partial HTML tags). At end-of-stream, the editor swaps to
  // the final clean HTML and the overlay disappears.
  const [streamText, setStreamText] = useState("");
  const refineAbortRef = useRef<AbortController | null>(null);

  async function handleRefine() {
    const instruction = refineInput.trim();
    if (!instruction || refining || !editor) return;
    setRefining(true);
    setRefineError(null);
    setStreamText("");
    const ctrl = new AbortController();
    refineAbortRef.current = ctrl;
    try {
      const res = await fetch(`/api/compose/saved-draft/${draftId}/refine`, {
        method:      "POST",
        credentials: "include",
        headers:     { "content-type": "application/json" },
        body:        JSON.stringify({ instruction }),
        signal:      ctrl.signal,
      });
      if (!res.ok || !res.body) {
        const txt = await res.text().catch(() => "");
        throw new Error(txt || `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      // accumulator (kept locally; we don't need React state per token)
      let accumulated = "";
      // Throttle stream UI updates so React doesn't re-render on
      // every byte. ~30 fps is more than enough to feel alive.
      let lastFlush = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          try {
            const ev = JSON.parse(payload);
            if (ev.phase === "text_delta" && typeof ev.text === "string") {
              accumulated += ev.text;
              const now = performance.now();
              if (now - lastFlush > 33) {
                setStreamText(accumulated);
                lastFlush = now;
              }
            } else if (ev.phase === "final") {
              setStreamText("");  // overlay off
              editor.commands.setContent(ev.body_html);
              setDraft(d => d ? { ...d, body_html: ev.body_html } : d);
              setRefineInput("");
              await loadVersions();
              if (ev.version_id) setHighlightedVersionId(ev.version_id);
            } else if (ev.phase === "error") {
              throw new Error(ev.error || "stream error");
            }
          } catch (parseErr: any) {
            if (parseErr.message && parseErr.message !== "stream error") {
              // JSON parse failures: skip malformed event, continue
              continue;
            }
            throw parseErr;
          }
        }
      }
    } catch (e: any) {
      if (e?.name !== "AbortError") {
        setRefineError(e?.message || "Refine failed");
      }
    } finally {
      setRefining(false);
      setStreamText("");
      refineAbortRef.current = null;
    }
  }

  function stopRefine() {
    refineAbortRef.current?.abort();
  }

  // ── Send controls ───────────────────────────────────────────────
  const [method, setMethod] = useState<SendMethod>(defaultMethodFor(initialKind || "letter"));
  const [pickedContact, setPickedContact] = useState<ContactHit | null>(null);
  const [sending, setSending] = useState(false);
  const [sendResult, setSendResult] = useState<SendResult | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);

  // Update method default once draft loads (in case it differs from hint)
  useEffect(() => {
    if (draft) setMethod(m => m === defaultMethodFor("letter") ? defaultMethodFor(draft.kind) : m);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft?.kind]);

  // Whether the chosen method is reachable for the picked contact.
  const methodReachable = useMemo(() => {
    if (method === "pdf") return true;
    if (!pickedContact) return false;
    const want = method === "email" ? "email" : "whatsapp";
    return pickedContact.channels.some(c => c.kind === want);
  }, [method, pickedContact]);

  async function handleSend() {
    if (sending) return;
    if (method !== "pdf" && !pickedContact) {
      setSendError("Pick a recipient first");
      return;
    }
    if (method !== "pdf" && !methodReachable) {
      setSendError(`${pickedContact!.display_name} has no ${method} channel — add one in Contacts first`);
      return;
    }
    setSending(true);
    setSendError(null);
    try {
      const r = await api.post<SendResult>(
        `/api/compose/saved-draft/${draftId}/send`,
        { method, recipient_id: pickedContact?.id ?? null,
          subject: draft?.subject ?? null },
      );
      setSendResult(r);
      if (method === "pdf" && r.pdf_url) {
        window.open(r.pdf_url, "_blank");
      }
    } catch (e: any) {
      setSendError(e?.message || "Send failed");
    } finally {
      setSending(false);
    }
  }

  // ── Render ──────────────────────────────────────────────────────
  const k = draft?.kind ?? initialKind ?? "letter";
  // Coerce to string defensively: compose_draft has historically saved
  // draft.recipient as a contact_id integer when the LLM passed it as
  // an int instead of a name string (the type lie that crashed
  // /r/chat for draft 52 — "Liebesbrief an Anna" — with
  // "TypeError: r.trim is not a function" inside RecipientPicker's
  // useState(initialQuery) ... initialQuery.toLowerCase().trim()
  // chain). String() makes 4070 → "4070" so .trim() / .toLowerCase()
  // are safe; the picker then matches it as best it can against
  // contact names and the user can correct via the picker UI.
  const _draftRecipient = draft?.recipient;
  const recipientLabel =
    pickedContact?.display_name
    || (_draftRecipient != null ? String(_draftRecipient) : "")
    || initialRecipient
    || "";
  const subjectLabel = draft?.subject || initialSubject || "";

  if (loadError) {
    return (
      <div className="mt-2 border border-red-500/30 bg-red-500/5 rounded-xl p-3 max-w-xl text-xs text-red-700 dark:text-red-300">
        Couldn't load draft #{draftId}: {loadError}
      </div>
    );
  }

  return (
    <div className="mt-2 border border-border rounded-xl bg-card/80 overflow-hidden max-w-xl">
      {/* ── Header ─────────────────────────────────────────────── */}
      <div className="px-3 pt-2.5 pb-2 border-b border-border/60">
        <div className="flex items-center gap-1.5 mb-1.5">
          <span className="text-base leading-none">{kindIcon(k)}</span>
          <span className="text-xs font-semibold">{KIND_LABEL[k] || "Dokument"}</span>
          <span className="text-[9px] text-muted-foreground font-mono">#{draftId}</span>
          {saveState === "saving" && (
            <span className="ml-auto text-[10px] text-muted-foreground inline-flex items-center gap-1">
              <Loader2 className="w-2.5 h-2.5 animate-spin" /> saving
            </span>
          )}
          {saveState === "saved" && (
            <span className="ml-auto text-[10px] text-emerald-600 inline-flex items-center gap-1">
              <Check className="w-2.5 h-2.5" /> saved
            </span>
          )}
          {saveState === "idle" && (
            <button
              onClick={() => navigate(`/compose?draft_id=${draftId}`)}
              className="ml-auto text-[10px] text-muted-foreground hover:text-foreground transition inline-flex items-center gap-1"
              title="Open in Compose for the full editor"
            >
              <ExternalLink className="w-2.5 h-2.5" /> Compose
            </button>
          )}
        </div>
        <div className="space-y-0.5 text-xs">
          {recipientLabel && (
            <div>
              <span className="text-muted-foreground">An:</span>{" "}
              <span className="font-medium">{recipientLabel}</span>
            </div>
          )}
          {subjectLabel && (
            <div>
              <span className="text-muted-foreground">Betreff:</span>{" "}
              <span className="font-medium">{subjectLabel}</span>
            </div>
          )}
          {(missingArgs?.length ?? 0) > 0 && (
            <div className="mt-1.5 text-[11px] text-amber-600 dark:text-amber-400 inline-flex items-center gap-1">
              <AlertCircle className="w-3 h-3" />
              Still empty: {missingArgs!.join(", ")} — open in Compose to fill in
            </div>
          )}
        </div>
        {templateName && (
          <div className="mt-1.5 text-[10px] text-muted-foreground">
            Vorlage: <span className="text-foreground/80 font-medium">{templateName}</span>
          </div>
        )}
      </div>

      {/* ── Version history chips ──────────────────────────────── */}
      {versions.length > 1 && (
        <div className="px-3 py-1.5 border-b border-border/40 flex items-center gap-1 overflow-x-auto bg-muted/30">
          <History className="w-3 h-3 text-muted-foreground shrink-0" />
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground shrink-0">
            History
          </span>
          {versions.map((v, idx) => {
            const isCurrent = v.id === highlightedVersionId;
            const labelTip = v.source === "refine" && v.instruction
              ? `"${v.instruction}"`
              : v.source === "restore"
                ? `Restored from v${versions.findIndex(x => x.id === v.restored_from) + 1 || "?"}`
                : v.source === "initial"
                  ? "Initial draft"
                  : v.source;
            return (
              <button
                key={v.id}
                onClick={() => !isCurrent && restoreVersion(v.id)}
                disabled={refining || isCurrent}
                title={labelTip}
                className={cn(
                  "text-[10px] px-1.5 py-0.5 rounded-full tabular-nums shrink-0 transition",
                  isCurrent
                    ? "bg-violet-500/20 text-violet-700 dark:text-violet-300 ring-1 ring-violet-500/40 font-semibold cursor-default"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  refining && "opacity-40",
                )}
              >
                v{idx + 1}
                {v.source === "restore" && <span className="ml-0.5 opacity-60">↺</span>}
              </button>
            );
          })}
          {versions.length > 1 && highlightedVersionId !== versions[versions.length - 1].id && (
            <button
              onClick={() => restoreVersion(versions[versions.length - 1].id)}
              disabled={refining}
              title="Jump to the most recent version"
              className="ml-1 text-[10px] px-1.5 py-0.5 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground inline-flex items-center gap-0.5 shrink-0"
            >
              <RotateCcw className="w-2.5 h-2.5" /> Latest
            </button>
          )}
        </div>
      )}

      {/* ── Editor (with streaming overlay) ────────────────────── */}
      {!draft ? (
        <div className="px-3 py-6 text-center text-xs text-muted-foreground">
          <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
          Loading draft…
        </div>
      ) : (
        <div className="relative border-y border-border/60 bg-background">
          <div className={cn(refining && "opacity-40 pointer-events-none transition-opacity")}>
            <EditorContent editor={editor} />
          </div>
          {refining && (
            <div className="absolute inset-0 overflow-y-auto p-3 bg-background/95 backdrop-blur-[2px]">
              <div className="flex items-center gap-1.5 text-[10px] text-violet-700 dark:text-violet-300 mb-1.5 uppercase tracking-wider">
                <Sparkles className="w-3 h-3 animate-pulse" /> LLM is rewriting…
                <button
                  onClick={stopRefine}
                  className="ml-auto text-red-600 hover:underline normal-case tracking-normal text-[11px]"
                >
                  Stop
                </button>
              </div>
              <div className="text-xs leading-relaxed whitespace-pre-wrap font-mono text-foreground/85">
                {streamText}
                <span className="inline-block w-1.5 h-3 ml-0.5 align-text-bottom bg-violet-500 animate-pulse" />
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── Refine ─────────────────────────────────────────────── */}
      <div className="px-3 py-2 border-b border-border/60 bg-muted/20">
        <div className="flex items-center gap-2">
          <Wand2 className="w-3 h-3 text-violet-500 shrink-0" />
          <input
            value={refineInput}
            onChange={e => setRefineInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === "Enter") { e.preventDefault(); handleRefine(); }
            }}
            disabled={refining || !draft}
            placeholder="Ask LLM to refine: 'make it shorter', 'use Sie form', …"
            className="flex-1 h-7 px-2 bg-background border border-border rounded-md text-xs focus:outline-none focus:ring-1 focus:ring-violet-500/40 placeholder:text-muted-foreground/60"
          />
          <button
            onClick={handleRefine}
            disabled={refining || !refineInput.trim()}
            className={cn(
              "text-[11px] h-7 px-2.5 rounded-md font-medium inline-flex items-center gap-1 transition",
              refining || !refineInput.trim()
                ? "bg-muted text-muted-foreground cursor-not-allowed"
                : "bg-violet-500/15 text-violet-700 dark:text-violet-300 hover:bg-violet-500/25",
            )}
          >
            {refining
              ? <><Loader2 className="w-3 h-3 animate-spin" /> Refining…</>
              : <><Sparkles className="w-3 h-3" /> Refine</>}
          </button>
        </div>
        {refineError && (
          <div className="mt-1 text-[10px] text-red-600">{refineError}</div>
        )}
      </div>

      {/* ── Send controls ──────────────────────────────────────── */}
      {sendResult?.ok ? (
        <div className="px-3 py-3 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 text-xs flex items-center gap-2">
          <Check className="w-4 h-4 shrink-0" />
          <div className="flex-1">
            <div className="font-medium">
              {sendResult.method === "pdf"
                ? "PDF rendered — opening in a new tab"
                : `Sent via ${sendResult.method} to ${sendResult.to}`}
            </div>
            {sendResult.method === "pdf" && sendResult.pdf_url && (
              <a href={sendResult.pdf_url} target="_blank" rel="noopener"
                 className="text-[11px] underline mt-0.5 inline-block">
                Re-open PDF
              </a>
            )}
          </div>
        </div>
      ) : (
        <div className="px-3 py-2.5 space-y-2">
          {/* Recipient picker — required for email/whatsapp */}
          {method !== "pdf" && (
            <RecipientInlinePicker
              initialQuery={recipientLabel}
              picked={pickedContact}
              onPick={setPickedContact}
            />
          )}

          {/* Method radio */}
          <div className="flex items-center gap-1">
            <MethodChip
              chosen={method === "email"}
              onClick={() => setMethod("email")}
              icon={<Mail className="w-3 h-3" />}
              label="Email"
              tint="sky"
              reachable={!!pickedContact?.channels.some(c => c.kind === "email")}
            />
            <MethodChip
              chosen={method === "whatsapp"}
              onClick={() => setMethod("whatsapp")}
              icon={<MessageCircle className="w-3 h-3" />}
              label="WhatsApp"
              tint="emerald"
              reachable={!!pickedContact?.channels.some(c => c.kind === "whatsapp")}
            />
            <MethodChip
              chosen={method === "pdf"}
              onClick={() => setMethod("pdf")}
              icon={<FileText className="w-3 h-3" />}
              label="PDF"
              tint="amber"
              reachable={true}
            />
            <button
              onClick={handleSend}
              disabled={sending || (method !== "pdf" && !pickedContact)}
              className={cn(
                "ml-auto px-3 py-1.5 rounded-md text-xs font-medium inline-flex items-center gap-1.5 transition shadow-sm",
                sending || (method !== "pdf" && !pickedContact)
                  ? "bg-muted text-muted-foreground cursor-not-allowed"
                  : "bg-violet-500 hover:bg-violet-600 text-white",
              )}
            >
              {sending
                ? <><Loader2 className="w-3 h-3 animate-spin" /> Sending…</>
                : <><Send className="w-3 h-3" /> Send</>}
            </button>
          </div>
          {sendError && (
            <div className="text-[11px] text-red-600 flex items-center gap-1">
              <AlertCircle className="w-3 h-3" /> {sendError}
            </div>
          )}
          {method !== "pdf" && pickedContact && !methodReachable && (
            <div className="text-[10px] text-amber-600">
              {pickedContact.display_name} has no {method} channel on file.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Sub: send method chip ───────────────────────────────────────────

function MethodChip({
  chosen, onClick, icon, label, tint, reachable,
}: {
  chosen:    boolean;
  onClick:   () => void;
  icon:      React.ReactNode;
  label:     string;
  tint:      "sky" | "emerald" | "amber";
  reachable: boolean;
}) {
  const tintClasses: Record<string, string> = {
    sky:     "bg-sky-500/15 text-sky-700 dark:text-sky-300 ring-sky-500/40",
    emerald: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 ring-emerald-500/40",
    amber:   "bg-amber-500/15 text-amber-700 dark:text-amber-300 ring-amber-500/40",
  };
  return (
    <button
      onClick={onClick}
      className={cn(
        "px-2 py-1 rounded-md text-[11px] inline-flex items-center gap-1 transition",
        chosen
          ? `${tintClasses[tint]} ring-1 font-medium`
          : "text-muted-foreground hover:bg-muted/60",
        !reachable && chosen && "opacity-70",
      )}
      title={!reachable ? "Contact has no channel of this type — pick another method or add the channel" : ""}
    >
      {icon} {label}
    </button>
  );
}

// ─── Sub: recipient picker ───────────────────────────────────────────

function RecipientInlinePicker({
  initialQuery, picked, onPick,
}: {
  initialQuery: string;
  picked:       ContactHit | null;
  onPick:       (c: ContactHit | null) => void;
}) {
  // Coerce to string at the seam so a non-string slip from the caller
  // (e.g. an int contact_id passed as draft.recipient before the
  // backend forces the field to a name string) can't crash the
  // picker. Mirrors the defensive String() at the call site above.
  const [query, setQuery] = useState(String(initialQuery ?? ""));
  const [open, setOpen] = useState(false);
  const [results, setResults] = useState<ContactHit[]>([]);
  const [loading, setLoading] = useState(false);

  // Debounced contact search
  useEffect(() => {
    const q = query.trim();
    if (!q || q.length < 2) { setResults([]); return; }
    let cancelled = false;
    setLoading(true);
    const id = window.setTimeout(async () => {
      try {
        const r = await api.get<ContactHit[]>(
          `/api/contacts?query=${encodeURIComponent(q)}&status=active&limit=5`,
        );
        if (!cancelled) setResults(r || []);
      } catch {
        if (!cancelled) setResults([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 220);
    return () => { cancelled = true; clearTimeout(id); };
  }, [query]);

  // Auto-search + auto-pick the top hit on first mount if there's a
  // recipient hint and we don't have one yet — saves the user a click
  // when the chat already knew who.
  const didAutoPickRef = useRef(false);
  useEffect(() => {
    if (didAutoPickRef.current) return;
    if (picked || !initialQuery || initialQuery.length < 3) return;
    if (results.length === 0) return;
    // Only auto-pick if the top result name is a clear match
    // (case-insensitive substring) — avoids picking a random "John"
    // when the user typed "Jonathan".
    const top = results[0];
    const q = initialQuery.toLowerCase().trim();
    if (top.display_name.toLowerCase().includes(q) || q.includes(top.display_name.toLowerCase())) {
      didAutoPickRef.current = true;
      onPick(top);
      setOpen(false);
    }
  }, [results, initialQuery, picked, onPick]);

  if (picked) {
    return (
      <div className="flex items-center gap-2 px-2 py-1.5 rounded-md bg-muted/40 border border-border/60 text-xs">
        <div className="flex-1 min-w-0">
          <div className="font-medium truncate">{picked.display_name}</div>
          <div className="text-[10px] text-muted-foreground truncate">
            {picked.channels.length === 0
              ? "no channels"
              : picked.channels
                  .filter(c => ["email", "phone", "whatsapp"].includes(c.kind))
                  .map(c => `${c.kind}: ${c.value}`)
                  .join(" · ") || "no email/whatsapp channels"}
          </div>
        </div>
        <button
          onClick={() => { onPick(null); setOpen(true); setQuery(""); }}
          className="p-1 hover:bg-muted rounded text-muted-foreground"
          title="Change recipient"
        >
          <Pencil className="w-3 h-3" />
        </button>
      </div>
    );
  }
  return (
    <div className="relative">
      <input
        value={query}
        onChange={e => { setQuery(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        placeholder="Search recipient…"
        className="w-full h-7 px-2 bg-background border border-border rounded-md text-xs focus:outline-none focus:ring-1 focus:ring-ring/40 placeholder:text-muted-foreground/60"
      />
      {open && (loading || results.length > 0) && (
        <div className="absolute z-50 left-0 right-0 top-full mt-1 bg-card border border-border rounded-md shadow-lg overflow-hidden max-h-[240px] overflow-y-auto">
          {loading && (
            <div className="px-2 py-1.5 text-[10px] text-muted-foreground">
              <Loader2 className="w-3 h-3 animate-spin inline" /> searching…
            </div>
          )}
          {results.map(c => (
            <button
              key={c.id}
              onClick={() => { onPick(c); setOpen(false); }}
              className="w-full text-left px-2 py-1.5 hover:bg-muted/60 border-b border-border/40 last:border-b-0"
            >
              <div className="text-xs font-medium">{c.display_name}</div>
              <div className="text-[10px] text-muted-foreground truncate">
                {c.channels
                  .filter(ch => ["email", "phone", "whatsapp"].includes(ch.kind))
                  .map(ch => `${ch.kind}: ${ch.value}`)
                  .join(" · ") || "no contact channels"}
              </div>
            </button>
          ))}
          {!loading && results.length === 0 && query.length >= 2 && (
            <div className="px-2 py-1.5 text-[10px] text-muted-foreground italic">
              No matches for "{query}".
            </div>
          )}
        </div>
      )}
    </div>
  );
}
