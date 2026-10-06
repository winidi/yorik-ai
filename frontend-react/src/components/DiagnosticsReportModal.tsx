/**
 * The review before a diagnostic report leaves the house.
 *
 * Opened from a thumbs-down or "Report a problem" in the chat: asks
 * the backend for a draft (backend/diagnostics/report.py), shows what
 * was replaced, lets the person shorten the question and the note (the
 * scrubbed text is what they edit), shows the exact bytes, and sends
 * only on their click. Nothing happens on close.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, ShieldCheck, X } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Draft {
  id: string;
  status: string;
  payload: any;
  scrub_summary: { replaced?: Record<string, number>; dropped_fields?: number; free_text_dropped?: boolean; level?: string; mentions?: string[] };
}

export function DiagnosticsReportModal({ conversationId, messageIdx, trigger, reason, note, onClose }: {
  conversationId: string; messageIdx: number; trigger: "thumbs_down" | "report_problem";
  reason?: string | null; note?: string | null; onClose: (sent: boolean) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [noteText, setNoteText] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<"sent" | "declined" | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const d = await api.post<Draft>("/api/diagnostics/reports/draft",
          { conversation_id: conversationId, message_idx: messageIdx, trigger, reason: reason || null, note: note || null });
        if (!alive) return;
        setDraft(d);
        setQuestion(d.payload?.question?.text || "");
        setNoteText(d.payload?.feedback?.note || "");
      } catch (e: any) {
        if (alive) setErr(e?.message || "Could not prepare the report.");
      }
    })();
    return () => { alive = false; };
  }, [conversationId, messageIdx, trigger, reason, note]);

  const replaced = draft?.scrub_summary?.replaced || {};
  const replacedText = Object.entries(replaced).filter(([, n]) => n > 0).map(([k, n]) => `${n} ${k}`).join(", ");
  const preview = draft ? { ...draft.payload, question: { ...(draft.payload.question || {}), text: question },
                            feedback: { ...(draft.payload.feedback || {}), note: noteText } } : null;

  async function send() {
    if (!draft) return;
    setBusy(true); setErr(null);
    try {
      await api.post(`/api/diagnostics/reports/${draft.id}/send`, { question, note: noteText });
      setDone("sent");
    } catch (e: any) {
      setErr(e?.message || "Could not send.");
    } finally { setBusy(false); }
  }

  async function decline() {
    if (draft) { try { await api.post(`/api/diagnostics/reports/${draft.id}/decline`, {}); } catch {} }
    setDone("declined");
    onClose(false);
  }

  return (
    <div className="fixed inset-0 z-[80] bg-black/50 flex items-center justify-center p-4" onClick={() => !busy && (done === "sent" ? onClose(true) : decline())}>
      <div className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-2xl max-h-[92vh] flex flex-col overflow-hidden" onClick={e => e.stopPropagation()}>
        <div className="px-5 pt-5 pb-3 flex items-start gap-3">
          <ShieldCheck className="w-5 h-5 text-violet-500 shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <div className="text-base font-semibold">{t("chat.diag.title", "Send a diagnostic report?")}</div>
            <div className="text-xs text-muted-foreground mt-0.5">
              {t("chat.diag.intro", "This is exactly what would leave this computer. People, addresses and chats are already numbers; the words of your mails and messages are never included. You can shorten the text; nothing is sent until you click Send.")}
            </div>
          </div>
          <button onClick={() => (done === "sent" ? onClose(true) : decline())} className="p-1 rounded hover:bg-muted" title="Close"><X className="w-4 h-4" /></button>
        </div>
        <div className="px-5 pb-4 overflow-y-auto space-y-3 flex-1 min-h-0">
          {!draft && !err && <div className="text-sm text-muted-foreground flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> {t("chat.diag.preparing", "Preparing the report …")}</div>}
          {err && <div className="text-sm text-red-500">{err}</div>}
          {draft && done !== "sent" && (
            <>
              <div className="text-xs rounded-lg border border-border bg-muted/30 p-3">
                <span className="font-medium">{t("chat.diag.replaced", "Replaced")}:</span> {replacedText || t("chat.diag.nothingReplaced", "nothing needed replacing")}
                {draft.scrub_summary?.dropped_fields ? ` · ${draft.scrub_summary.dropped_fields} ${t("chat.diag.dropped", "fields left out")}` : ""}
                {draft.scrub_summary?.free_text_dropped && (
                  <span className="block mt-1 text-amber-600">{t("chat.diag.freeTextDropped", "Your question's words were left out: your model runs in a cloud, so no local model could check it for names.")}</span>
                )}
              </div>
              {!draft.scrub_summary?.free_text_dropped && (
                <label className="block text-xs">
                  <span className="text-muted-foreground">{t("chat.diag.question", "Your question, as it would be sent (shorten it if you like)")}</span>
                  <input value={question} onChange={e => setQuestion(e.target.value.slice(0, 400))}
                         className="mt-1 w-full h-9 px-3 rounded-md bg-muted/40 border border-border text-sm" />
                </label>
              )}
              <label className="block text-xs">
                <span className="text-muted-foreground">{t("chat.diag.note", "What went wrong, in your words (optional)")}</span>
                <input value={noteText} onChange={e => setNoteText(e.target.value.slice(0, 300))}
                       placeholder={t("chat.diag.notePlaceholder", "e.g. it showed last month's order, not the newest")}
                       className="mt-1 w-full h-9 px-3 rounded-md bg-muted/40 border border-border text-sm" />
              </label>
              <details className="text-xs">
                <summary className="cursor-pointer text-muted-foreground">{t("chat.diag.exact", "The exact report")}</summary>
                <textarea readOnly value={JSON.stringify(preview, null, 2)} className="mt-2 w-full h-56 font-mono text-2xs p-3 rounded-md bg-muted/30 border border-border" />
              </details>
            </>
          )}
          {done === "sent" && (
            <div className="text-sm">{t("chat.diag.sentText", "Thank you. The report is queued and leaves with the next send; you can see it under Settings → Developer → Diagnostics.")}</div>
          )}
        </div>
        <div className="border-t border-border px-5 py-3 bg-muted/20 flex items-center justify-end gap-2">
          {done === "sent" ? (
            <button onClick={() => onClose(true)} className="px-4 h-9 rounded-md bg-primary text-primary-foreground text-sm">{t("common.close", "Close")}</button>
          ) : (
            <>
              <button onClick={decline} disabled={busy} className="px-3 h-9 rounded-md border border-border text-sm hover:bg-muted">{t("chat.diag.dontSend", "Don't send")}</button>
              <button onClick={send} disabled={busy || !draft}
                      className={cn("px-4 h-9 rounded-md text-sm text-white bg-gradient-to-r from-violet-500 to-blue-500 hover:from-violet-600 hover:to-blue-600", (busy || !draft) && "opacity-60")}>
                {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : t("chat.diag.send", "Send this report")}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
