/**
 * WhatsAppDraftCard — what the chat shows when whatsapp_draft wrote a
 * message: recipient, the draft in an editable box, and two ways on.
 *
 *   - "Senden" posts to /api/whatsapp/chats/{jid}/send — only on the
 *     user's click, never by itself.
 *   - "Im WhatsApp-Chat öffnen" jumps to the thread with the draft
 *     already in the composer (handed over via sessionStorage, which
 *     the Thread reads when it opens that jid). Hidden for a brand-new
 *     conversation: there is no thread to open until the first send.
 *
 * Before this card existed the chat said "the draft is in a card below"
 * and there was none — the text only reached the LLM.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { MessageCircle, ArrowRight, Send, Check } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "@/components/Toast";

export const WA_PREFILL_KEY = (jid: string) => `wa-prefill:${jid}`;

export function WhatsAppDraftCard({ chatJid, recipient, text: initialText, isNewChat, sentText, onSent }: {
  chatJid: string; recipient: string; text: string; isNewChat?: boolean;
  /** Set when this draft was already sent (remembered on the message):
   *  the card shows what went out, with no Send button. */
  sentText?: string | null;
  onSent?: (text: string) => void;
}) {
  const navigate = useNavigate();
  const { t: tr } = useTranslation();
  const [text, setText] = useState(sentText ?? initialText);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(sentText != null);
  const number = chatJid.endsWith("@s.whatsapp.net") ? "+" + chatJid.split("@")[0] : "";

  async function send() {
    const t = text.trim();
    if (!t || sending) return;
    setSending(true);
    try {
      await api.post(`/api/whatsapp/chats/${encodeURIComponent(chatJid)}/send`, { text: t });
      setSent(true);
      onSent?.(t);
    } catch (e: any) {
      toast(tr("chat.whatsapp.sendFailed", { error: e?.message || String(e) }), "error");
    } finally {
      setSending(false);
    }
  }

  function openChat() {
    try { sessionStorage.setItem(WA_PREFILL_KEY(chatJid), text); } catch { /* prefill is a convenience */ }
    navigate(`/whatsapp?chat=${encodeURIComponent(chatJid)}`);
  }

  return (
    <div className="mt-2 max-w-xl rounded-xl border border-border bg-card p-4">
      <div className="flex items-start gap-3">
        <span className="grid place-items-center w-9 h-9 rounded-lg bg-primary/15 text-primary shrink-0">
          <MessageCircle className="w-5 h-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="font-semibold truncate">{tr("chat.whatsapp.to", { name: recipient || number || tr("chat.whatsapp.thisChat") })}</div>
          {number && recipient && <div className="text-xs text-muted-foreground truncate">{number}</div>}
        </div>
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        disabled={sent}
        rows={Math.min(8, Math.max(2, text.split("\n").length + 1))}
        className="mt-3 w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm disabled:opacity-70"
      />
      <div className="mt-3 flex flex-wrap justify-end gap-2">
        {!isNewChat && (
          <button
            onClick={openChat}
            className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm font-medium hover:bg-muted"
          >
            {tr("chat.whatsapp.openChat")} <ArrowRight className="w-4 h-4" />
          </button>
        )}
        {sent ? (
          <span className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium text-primary">
            <Check className="w-4 h-4" /> {tr("chat.whatsapp.sent")}
          </span>
        ) : (
          <button
            onClick={send}
            disabled={sending || !text.trim()}
            className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium disabled:opacity-50"
          >
            <Send className="w-4 h-4" /> {sending ? tr("chat.whatsapp.sending") : tr("chat.whatsapp.send")}
          </button>
        )}
      </div>
    </div>
  );
}
