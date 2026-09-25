/**
 * A file the user showed Yorik in this chat ("Anhang #12" in the user's
 * message). It belongs to the conversation: filed in Paperless on the
 * user's yes, otherwise deleted with the chat or after 30 days. There
 * is no other place where unfiled attachments live.
 * Backend: backend/chat_attachments.py.
 */
import { useCallback, useEffect, useState } from "react";
import { Check, FileText, Image as ImageIcon, Loader2, Lock, Trash2, UserCheck, Users } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Attachment {
  id: number; filename: string; mime_type: string; bytes: number; is_image: boolean;
  expires_at: string; filed: boolean; visibility: string | null; suggest: "file" | "keep"; raw_url: string;
  default_visibility: "private" | "parents" | "shared" | "business";
  paperless_error?: string | null;
}

const WHO: Record<string, string> = {
  private: "nur für dich sichtbar", parents: "für die Eltern sichtbar",
  shared: "für die ganze Familie sichtbar", business: "für die Firma sichtbar",
};
type FileAs = "private" | "parents" | "shared";
const FILE_AS: Array<{ v: FileAs; label: string; hint: string }> = [
  { v: "private", label: "nur mich", hint: "Nur du siehst das Dokument unter Dokumente." },
  { v: "parents", label: "die Eltern", hint: "Die Erwachsenen im Haushalt, nicht die Konten der Kinder." },
  { v: "shared", label: "die Familie", hint: "Alle im Haushalt, auch die Konten der Kinder." },
];

/** Attachment numbers named in a chat message. */
export function attachmentIdsIn(text: string | null | undefined): number[] {
  const ids = new Set<number>();
  for (const m of (text || "").matchAll(/(?:Anhang|attachment)\s*#(\d+)/gi)) ids.add(parseInt(m[1], 10));
  return [...ids];
}

function size(bytes: number): string {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export function AttachmentCard({ id }: { id: number }) {
  const [att, setAtt] = useState<Attachment | null>(null);
  const [gone, setGone] = useState(false);
  const [busy, setBusy] = useState<FileAs | "delete" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setAtt(await api.get<Attachment>(`/api/chat/attachments/${id}`)); } catch { setGone(true); }
  }, [id]);
  useEffect(() => { load(); }, [load]);
  // The assistant may file it by skill ("ja, leg das ab"): follow along.
  useEffect(() => {
    const on = (e: Event) => { if ((e as CustomEvent).detail?.table === "chat_attachments") load(); };
    window.addEventListener("yorik-ui-action", on);
    return () => window.removeEventListener("yorik-ui-action", on);
  }, [load]);

  // Filing always says who may see the document in Paperless: only
  // the person, or the whole household.
  async function fileIt(visibility: FileAs) {
    setBusy(visibility); setError(null);
    try { setAtt(await api.post<Attachment>(`/api/chat/attachments/${id}/file?visibility=${visibility}`, {})); }
    catch (e: any) { setError(e?.message || "Die Datei konnte nicht abgelegt werden."); }
    finally { setBusy(null); }
  }
  async function remove() {
    if (!confirm("Diesen Anhang löschen? Er ist nicht unter Dokumente abgelegt.")) return;
    setBusy("delete");
    try { await api.delete(`/api/chat/attachments/${id}`); setGone(true); } catch { /* stays */ } finally { setBusy(null); }
  }

  if (gone) {
    return <div className="mt-1.5 text-xs text-muted-foreground italic">Anhang #{id} ist nicht mehr da (gelöscht oder abgelaufen).</div>;
  }
  if (!att) return null;
  const until = new Date(att.expires_at).toLocaleDateString([], { day: "numeric", month: "long" });

  return (
    <div className="mt-2 w-full max-w-sm rounded-xl border border-border bg-card text-card-foreground text-left overflow-hidden">
      <a href={att.raw_url} target="_blank" rel="noreferrer" className="flex items-center gap-3 p-3 hover:bg-muted/40 transition">
        {att.is_image
          ? <img src={att.raw_url} alt="" className="w-12 h-12 rounded-md object-cover shrink-0 bg-muted" />
          : <span className="w-12 h-12 rounded-md bg-amber-500/10 text-amber-600 grid place-items-center shrink-0"><FileText className="w-5 h-5" /></span>}
        <span className="min-w-0">
          <span className="block text-sm font-medium truncate">{att.filename}</span>
          <span className="block text-xs text-muted-foreground">{att.is_image ? <ImageIcon className="inline w-3 h-3 mr-1 -mt-0.5" /> : null}{size(att.bytes)}</span>
        </span>
      </a>
      <div className="px-3 pb-3">
        {att.filed ? (
          <div className="flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400">
            <Check className="w-3.5 h-3.5" /> In Dokumente abgelegt{att.visibility ? `, ${WHO[att.visibility] || att.visibility}` : ""} ·{" "}
            <a href="/r/documents" className="underline">Dokumente öffnen</a>
          </div>
        ) : (
          <>
            <div className="text-xs text-muted-foreground mb-2">Nur in diesem Gespräch · wird am {until} gelöscht</div>
            <div className="text-xs font-medium mb-1">In Dokumente ablegen, sichtbar für</div>
            <div className="flex flex-wrap gap-2">
              {FILE_AS.map(({ v, label, hint }) => {
                const primary = att.suggest === "file" && att.default_visibility === v;
                return (
                  <button key={v} onClick={() => fileIt(v)} disabled={!!busy} title={hint}
                          className={cn("inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition disabled:opacity-60",
                                        primary ? "bg-primary text-primary-foreground" : "border border-border hover:bg-muted")}>
                    {busy === v ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : v === "private" ? <Lock className="w-3.5 h-3.5" /> : v === "parents" ? <UserCheck className="w-3.5 h-3.5" /> : <Users className="w-3.5 h-3.5" />}
                    {label}
                  </button>
                );
              })}
              <button onClick={remove} disabled={!!busy} title="Anhang jetzt löschen"
                      className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs border border-border text-muted-foreground hover:text-foreground hover:bg-muted transition disabled:opacity-60 ml-auto">
                {busy === "delete" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                Löschen
              </button>
            </div>
            {(error || att.paperless_error) && (
              <div className="mt-2 text-xs text-red-500">
                {error || "Die Datei konnte nicht abgelegt werden."}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
