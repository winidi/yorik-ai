/**
 * Inline "Ask Yorik about this draft" panel — sits at the bottom of
 * the Compose editor section. Collapsed by default; click to expand.
 *
 * Contract with the agent:
 *   - We prefix every user message with a [Compose context: …] line
 *     so the agent knows the user is editing a document and what
 *     template/recipient/body they have so far.
 *   - The compose_draft skill is the canonical write path; the agent
 *     calls it with `template_id` + (ideally) `contact_id`, which
 *     persists a new compose_drafts row and emits a
 *     `compose_draft_created` ui_action. We intercept that locally
 *     and call `onDraftLoaded(draftId)` so the parent updates the
 *     CURRENT editor in-place — no navigation.
 *   - Multi-turn: a stable conversation_id is kept per Compose session
 *     so ambiguity ("Welcher Hans?") and follow-up answers work.
 *
 * What we DON'T do here:
 *   - Render every ui_action (template_pickers, show_calendar, …).
 *     The Compose panel cares only about compose_draft_created;
 *     anything else still bubbles up via emitUiAction so the global
 *     NavigationBridge picks it up.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Send, Loader2, Sparkles, ChevronDown, ChevronUp, X, Mic,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { emitUiAction } from "@/lib/uiActions";
import { useAuth } from "@/components/AuthGate";
import type { AskResponse } from "../chat/types";
import { NeedsInputCard, type NeedsInputAction } from "./NeedsInputCard";
import { AssistantMarkdown } from "@/components/AssistantMarkdown";
import { PhotoPickerCard, type PhotoPickerAction } from "./PhotoPickerCard";

interface Msg {
  role: "user" | "assistant";
  content: string;
  /** Tag the assistant message that drove a draft update so the UI can
   *  hint "I updated your draft". */
  drafted?: boolean;
  /** Embedded needs_input form rendered inline as part of this message. */
  needsInput?: NeedsInputAction;
  /** Embedded photo picker rendered inline as part of this message. */
  photoPicker?: PhotoPickerAction;
}

interface Props {
  /** Currently-picked template id, or null when starting from blank. */
  templateId: string | null;
  /** Template name for context summary. */
  templateName: string | null;
  /** Args (recipient, subject, etc.) the user has currently. */
  args: Record<string, unknown>;
  /** Editor body as HTML — sent in the context summary, capped. */
  bodyHtml: string;
  /** Id of the currently-loaded saved draft (when any). Surfaced to the
   *  LLM in the context line as `draft_id=N` so chat-driven edits go to
   *  compose_draft(existing_draft_id=N) — updates in place instead of
   *  creating a new draft on every "ändere X" request. */
  draftId: number | null;
  /** Called with the draft_id of any compose_draft_created action so
   *  the parent can fetch + load it into the editor in-place. */
  onDraftLoaded: (draftId: number) => void;
  /** Toast for transient feedback (e.g. errors). */
  toast: (text: string, kind?: "info" | "success" | "error") => void;
  /** Active role for /api/ask. */
  role: string;
  /** When true, render as a column-filling component (no collapse,
   *  no bottom-strip styling). Used when hosted inside the right pane
   *  via the Arguments/Ask tabs. */
  embedded?: boolean;
  /** Fires every time a new assistant reply lands. Lets the parent
   *  show an unread-message dot on the inactive Ask tab. */
  onAssistantMessage?: () => void;
}

export function ComposeAgentChat({
  templateId, templateName, args, bodyHtml, draftId, onDraftLoaded, toast, role,
  embedded = false, onAssistantMessage,
}: Props) {
  // Open by default — the inline chat IS the main interaction surface
  // for non-template work, hiding it behind a click made it invisible.
  // The user can still collapse it via the chevron if they want
  // full-height editing room.
  const [open, setOpen] = useState(true);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  // Sender identity from the auth context — feeding it into the
  // [Compose context:] line means the LLM never has to ask the user
  // "what's your name and address?" for the letterhead. It's already
  // sitting in user_profiles after onboarding.
  const { user } = useAuth();
  // Conversation id is stable for the whole compose session so the
  // agent's prior find_contact ambiguity result is still in context
  // when the user disambiguates on the next turn.
  const conversationIdRef = useRef<string>(
    `compose:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`,
  );

  const scrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (open) scrollRef.current?.scrollTo({ top: 9999, behavior: "smooth" });
  }, [msgs, open]);

  const contextLine = useMemo(() => {
    // Strip HTML for a cheap body summary; cap so we don't blow tokens.
    const plainBody = bodyHtml
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    const bodyExcerpt = plainBody.length > 600
      ? plainBody.slice(0, 600) + "…"
      : plainBody;
    const argSummary = Object.entries(args)
      .filter(([, v]) => v != null && String(v).trim() !== "")
      .map(([k, v]) => `${k}=${String(v).slice(0, 80)}`)
      .join(", ");
    // Sender block — composed once so the LLM has a single string to
    // splat into the letterhead args. We send first+last separately
    // AND a combined display so templates can pick whichever they need.
    const senderName = [user.first_name, user.last_name].filter(Boolean).join(" ")
      || user.name || "";
    const senderAddrBits = [
      user.address_street,
      [user.address_postcode, user.address_city].filter(Boolean).join(" "),
      user.country,
    ].filter(Boolean);
    const senderAddr = senderAddrBits.join(", ");
    const senderBits: string[] = [];
    if (senderName) senderBits.push(`sender_name="${senderName}"`);
    if (user.first_name) senderBits.push(`sender_first_name="${user.first_name}"`);
    if (user.last_name)  senderBits.push(`sender_last_name="${user.last_name}"`);
    if (senderAddr) senderBits.push(`sender_address="${senderAddr}"`);
    if (user.phone) senderBits.push(`sender_phone="${user.phone}"`);
    if (user.business_name) senderBits.push(`sender_business="${user.business_name}"`);
    // Always include a binary signature_on_file marker so the LLM can
    // confidently say "your scanned signature will appear above the
    // typed name" (or, when missing, deep-link the user to Settings).
    senderBits.push(`signature_on_file=${user.signature_data_url ? "yes" : "no"}`);
    // Always include sender_address_status so the LLM can detect empty
    // profile addresses without parsing the absence of a key.
    senderBits.push(`sender_address_status=${senderAddr ? "complete" : "missing"}`);
    const senderBlock = senderBits.length ? ` | sender: ${senderBits.join(", ")}` : "";

    return [
      "[Compose context:",
      templateId ? ` template=${templateId}${templateName ? ` (${templateName})` : ""}` : " no template",
      draftId ? ` | draft_id=${draftId} (pass as existing_draft_id when calling compose_draft so this draft is UPDATED, not duplicated)` : "",
      argSummary ? ` | args: ${argSummary}` : "",
      senderBlock,
      bodyExcerpt ? ` | body so far: "${bodyExcerpt}"` : " | body empty",
      "]",
    ].join("");
  }, [templateId, templateName, args, bodyHtml, user, draftId]);

  const send = useCallback(async () => {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    setMsgs(m => [...m, { role: "user", content: t }]);
    setText("");
    try {
      const wrapped = `${contextLine} ${t}`;
      const r = await api.post<AskResponse>("/api/ask", {
        message: wrapped,
        role,
        conversation_id: conversationIdRef.current,
        // Every message from this panel is action-shaped — the user is
        // asking to modify the draft. Forcing the LLM to make a tool
        // call on iteration 1 eliminates the "Ich muss Hans finden,
        // bevor ich..." narration failure mode where the loop ends
        // with no work done.
        require_tool_call: true,
      });
      // Locally intercept compose_draft_created (update editor in-place)
      // and needs_input (render an inline form on the next assistant
      // bubble). Everything else propagates up to the global bus so
      // navigate / show_calendar etc still work.
      let drafted = false;
      let needsInput: NeedsInputAction | undefined;
      let photoPicker: PhotoPickerAction | undefined;
      for (const a of r.ui_actions || []) {
        if (a.type === "compose_draft_created" && typeof a.draft_id === "number") {
          onDraftLoaded(a.draft_id);
          drafted = true;
        } else if (a.type === "needs_input") {
          needsInput = a as unknown as NeedsInputAction;
        } else if (a.type === "photo_picker") {
          photoPicker = a as unknown as PhotoPickerAction;
        } else {
          emitUiAction(a);
        }
      }
      const replyText = (r.response || "").trim() || (drafted
        ? "Drafted — siehst du gleich oben im Editor."
        : photoPicker
          ? "Welches Foto soll rein?"
          : needsInput
            ? "Mir fehlt noch eine Info — siehe Formular unten."
            : "(no reply)");
      setMsgs(m => [...m, { role: "assistant", content: replyText, drafted, needsInput, photoPicker }]);
      onAssistantMessage?.();
      if (!open) setOpen(true);  // pop open if the user collapsed it
    } catch (e: any) {
      const detail = e?.message || String(e);
      setMsgs(m => [...m, { role: "assistant", content: `Failed: ${detail}` }]);
      toast(`Ask failed: ${detail}`, "error");
    } finally {
      setBusy(false);
    }
  }, [text, busy, contextLine, role, onDraftLoaded, toast, open]);

  function clearConversation() {
    setMsgs([]);
    conversationIdRef.current = `compose:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
  }

  // Form-submit from NeedsInputCard: push the synthesised message as a
  // user turn AND send it to /api/ask exactly like a typed message. The
  // LLM playbook picks it up and runs compose_draft as the next step.
  const submitNeedsInput = useCallback(async (resumeMessage: string) => {
    if (busy) return;
    setBusy(true);
    setMsgs(m => [...m, { role: "user", content: resumeMessage }]);
    try {
      const wrapped = `${contextLine} ${resumeMessage}`;
      const r = await api.post<AskResponse>("/api/ask", {
        message: wrapped,
        role,
        conversation_id: conversationIdRef.current,
        require_tool_call: true,
      });
      let drafted = false;
      let needsInput: NeedsInputAction | undefined;
      let photoPicker: PhotoPickerAction | undefined;
      for (const a of r.ui_actions || []) {
        if (a.type === "compose_draft_created" && typeof a.draft_id === "number") {
          onDraftLoaded(a.draft_id);
          drafted = true;
        } else if (a.type === "needs_input") {
          needsInput = a as unknown as NeedsInputAction;
        } else if (a.type === "photo_picker") {
          photoPicker = a as unknown as PhotoPickerAction;
        } else {
          emitUiAction(a);
        }
      }
      const replyText = (r.response || "").trim() || (drafted
        ? "Drafted — siehst du gleich oben im Editor."
        : "(no reply)");
      setMsgs(m => [...m, { role: "assistant", content: replyText, drafted, needsInput, photoPicker }]);
      onAssistantMessage?.();
    } catch (e: any) {
      const detail = e?.message || String(e);
      setMsgs(m => [...m, { role: "assistant", content: `Failed: ${detail}` }]);
      toast(`Ask failed: ${detail}`, "error");
    } finally {
      setBusy(false);
    }
  }, [busy, contextLine, role, onDraftLoaded, toast, onAssistantMessage]);

  // Collapsed bar: single-line CTA. Only relevant for the legacy
  // bottom-of-editor placement (`embedded=false`). When hosted in the
  // right pane via the Arguments/Ask tab, we never collapse — the tab
  // itself IS the show/hide affordance.
  if (!open && !embedded) {
    return (
      <div className="border-t border-border bg-muted/30 px-4 py-2 shrink-0">
        <button
          onClick={() => setOpen(true)}
          className="w-full flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground transition"
        >
          <Sparkles className="w-3.5 h-3.5 text-violet-500" />
          <span>Ask Yorik about this draft…</span>
          <ChevronUp className="w-3.5 h-3.5 ml-auto" />
        </button>
      </div>
    );
  }

  return (
    <div className={cn(
      "flex flex-col",
      embedded
        ? "flex-1 min-h-0 bg-card"
        : "border-t border-border bg-background shrink-0",
    )}
         style={embedded ? undefined : { maxHeight: "420px" }}>
      {/* Header — embedded mode skips it (the tab label already says
          "Ask Yorik"). The reset button keeps a home on the top-right. */}
      {!embedded && (
        <div className="flex items-center gap-2 px-4 py-2 border-b border-border bg-card/40">
          <div className="w-7 h-7 rounded-full bg-gradient-to-br from-violet-500/30 to-blue-500/30 flex items-center justify-center">
            <Sparkles className="w-3.5 h-3.5 text-violet-500" />
          </div>
          <div className="text-sm font-medium">Ask Yorik about this draft</div>
          <div className="ml-auto flex items-center gap-1">
            {msgs.length > 0 && (
              <button
                onClick={clearConversation}
                className="text-[10px] text-muted-foreground hover:text-foreground px-2 py-1 rounded hover:bg-muted/60 inline-flex items-center gap-1"
                title="Reset conversation"
              >
                <X className="w-3 h-3" /> reset
              </button>
            )}
            <button
              onClick={() => setOpen(false)}
              className="text-muted-foreground hover:text-foreground p-2 md:p-1 rounded hover:bg-muted/60 inline-flex items-center justify-center"
              title="Collapse"
              aria-label="Collapse Ask Yorik panel"
            >
              <ChevronDown className="w-5 h-5 md:w-4 md:h-4" />
            </button>
          </div>
        </div>
      )}
      {embedded && msgs.length > 0 && (
        <div className="px-3 pt-2 flex justify-end">
          <button
            onClick={clearConversation}
            className="text-[10px] text-muted-foreground hover:text-foreground px-2 py-1 rounded hover:bg-muted/60 inline-flex items-center gap-1"
            title="Reset conversation"
          >
            <X className="w-3 h-3" /> reset
          </button>
        </div>
      )}

      {/* Messages — ChatApp-style avatars + rounded bubbles */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-3 space-y-3 min-h-[120px]">
        {msgs.length === 0 && (
          <div className="text-xs text-muted-foreground italic py-4 text-center max-w-md mx-auto leading-relaxed">
            Tell Yorik what this letter should say. He'll use the current template,
            pull recipient details from your contacts, and ask if anything is missing.
          </div>
        )}
        {msgs.map((m, i) => (
          <ChatBubble
            key={i}
            message={m}
            onSubmitNeedsInput={submitNeedsInput}
            toast={toast}
          />
        ))}
        {busy && (
          <div className="flex gap-3">
            <div className="w-9 h-9 rounded-full bg-gradient-to-br from-violet-500/30 to-blue-500/30 flex items-center justify-center shrink-0 mt-0.5">
              <Sparkles className="w-4 h-4 text-violet-500" />
            </div>
            <div className="rounded-2xl rounded-tl-md bg-card border border-border px-4 py-2.5 text-sm text-muted-foreground flex items-center gap-2">
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> Yorik is thinking…
            </div>
          </div>
        )}
      </div>

      {/* Composer */}
      <form
        onSubmit={(e) => { e.preventDefault(); send(); }}
        className="border-t border-border px-4 py-3 flex gap-2 items-end bg-card/40"
      >
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
          }}
          placeholder='What should this letter say? E.g. "tell my landlord I need a parking spot from June"'
          rows={1}
          disabled={busy}
          className="flex-1 resize-none text-sm bg-background border border-border rounded-xl px-3 py-2 focus:outline-none focus:ring-1 focus:ring-ring/40 max-h-32 disabled:opacity-50"
          style={{ minHeight: 40 }}
        />
        {/* Voice handoff — dispatches the same event the chat-route mic
            button uses, so the existing VoiceFab opens its popover and
            transcribes. No duplicate recording logic in this component. */}
        <button
          type="button"
          onClick={() => window.dispatchEvent(new CustomEvent("yorik:voice:start"))}
          disabled={busy}
          title="Speak instead — Yorik opens the voice popover"
          className="h-10 w-10 rounded-xl text-muted-foreground hover:text-foreground hover:bg-muted transition flex items-center justify-center shrink-0 disabled:opacity-50"
        >
          <Mic className="w-4 h-4" />
        </button>
        <button
          type="submit"
          disabled={!text.trim() || busy}
          className="h-10 px-4 rounded-xl bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5"
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
          Send
        </button>
      </form>
    </div>
  );
}

function ChatBubble({
  message,
  onSubmitNeedsInput,
  toast,
}: {
  message: Msg;
  onSubmitNeedsInput: (resumeMessage: string) => void;
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const isUser = message.role === "user";
  return (
    <div className={cn("flex gap-3 group", isUser ? "flex-row-reverse" : "flex-row")}>
      <div className={cn(
        "w-9 h-9 rounded-full shrink-0 flex items-center justify-center mt-0.5",
        isUser
          ? "bg-gradient-to-br from-blue-500 to-violet-500 text-white"
          : "bg-gradient-to-br from-violet-500/30 to-blue-500/30",
      )}>
        {isUser
          ? <span className="text-xs font-semibold">You</span>
          : <Sparkles className="w-4 h-4 text-violet-500" />}
      </div>
      <div className={cn("max-w-[78%] min-w-0 space-y-2", isUser && "items-end flex flex-col")}>
        {!isUser && message.drafted && (
          <div className="text-[9px] uppercase tracking-wider text-emerald-600 font-semibold mb-1 ml-1">
            ✓ Editor updated
          </div>
        )}
        <div className={cn(
          "rounded-2xl px-4 py-3 text-[15px] leading-[1.55] break-words",
          isUser
            ? "bg-violet-500 text-white rounded-tr-md whitespace-pre-wrap"
            : message.drafted
              ? "bg-emerald-500/10 border border-emerald-500/30 rounded-tl-md"
              : "bg-card border border-border rounded-tl-md",
        )}>
          {isUser
            ? message.content
            : <AssistantMarkdown>{message.content}</AssistantMarkdown>}
        </div>
        {!isUser && message.needsInput && (
          <NeedsInputCard
            action={message.needsInput}
            onSubmit={onSubmitNeedsInput}
            toast={toast}
          />
        )}
        {!isUser && message.photoPicker && (
          <PhotoPickerCard
            action={message.photoPicker}
            onSubmit={onSubmitNeedsInput}
          />
        )}
      </div>
    </div>
  );
}
