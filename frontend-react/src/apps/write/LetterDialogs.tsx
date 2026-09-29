/**
 * The two ways a letter leaves the house: by mail from one of your own
 * accounts (the PDF attached), or into Paperless under your own name.
 * Either makes the letter final, and the dialogs say so before.
 */
import { useEffect, useState } from "react";
import { Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import i18n from "@/i18n";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { WrittenDoc } from "./types";

type Account = { id: number; email: string; display_name?: string | null; is_default?: boolean | number; enabled?: boolean | number };

function Shell({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const { t } = useTranslation();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 grid place-items-center p-4 bg-black/60" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label={title} onClick={e => e.stopPropagation()} className="w-full max-w-md rounded-2xl bg-card border border-border shadow-2xl p-5 grid gap-3">
        <div className="flex items-center justify-between"><h2 className="font-semibold">{title}</h2>
          <button onClick={onClose} aria-label={t("common.close")} className="p-1.5 rounded-md hover:bg-muted"><X className="w-4 h-4" /></button></div>
        {children}
      </div>
    </div>
  );
}

const field = "w-full rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm outline-none focus:border-primary";
const finalNote = (doc: WrittenDoc) => doc.status === "draft"
  ? <p className="text-xs text-muted-foreground">{i18n.t("write.finalNote")}</p> : null;

export function SendDialog({ doc, to, subject, onClose, onDone }: { doc: WrittenDoc; to: string; subject: string; onClose: () => void; onDone: (d: WrittenDoc) => void }) {
  const { t } = useTranslation();
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [addr, setAddr] = useState(to);
  const [subj, setSubj] = useState(subject);
  const [message, setMessage] = useState("Guten Tag,\n\nanbei erhalten Sie mein Schreiben als PDF.\n\nMit freundlichen Grüßen");
  // A letter can also BE the mail (text, closing, name) instead of a PDF
  // attached to one; the last choice is kept on this device.
  const canText = doc.kind === "letter";
  const [sendAs, setSendAs] = useState<"pdf" | "text">(() => {
    try { return canText && localStorage.getItem("yorik_write_send_as") === "text" ? "text" : "pdf"; } catch { return "pdf"; }
  });
  function chooseSendAs(v: "pdf" | "text") {
    setSendAs(v);
    try { localStorage.setItem("yorik_write_send_as", v); } catch {}
  }
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    api.get<Account[]>("/api/email/accounts").then(list => {
      const usable = list.filter(a => a.enabled !== false && a.enabled !== 0);
      setAccounts(usable); setAccountId((usable.find(a => a.is_default) || usable[0])?.id ?? null);
    }).catch(() => setAccounts([]));
  }, []);
  async function send() {
    if (!accountId) return;
    setBusy(true); setError("");
    try { onDone((await api.post<{ document: WrittenDoc }>(`/api/writing/${doc.id}/send`, { account_id: accountId, to: addr, subject: subj, message, send_as: canText ? sendAs : "pdf" })).document); }
    catch (e: any) { setError(e?.message || String(e)); }
    finally { setBusy(false); }
  }
  return (
    <Shell title={t("write.sendByEmail")} onClose={onClose}>
      {accounts === null ? <Loader2 className="w-5 h-5 animate-spin text-muted-foreground" /> : accounts.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("write.noAccount")}</p>
      ) : (
        <>
          <label className="grid gap-1 text-xs text-muted-foreground">{t("write.from")}
            <select value={accountId ?? ""} onChange={e => setAccountId(Number(e.target.value))} className={field}>
              {accounts.map(a => <option key={a.id} value={a.id}>{a.display_name ? `${a.display_name} · ${a.email}` : a.email}</option>)}
            </select></label>
          <label className="grid gap-1 text-xs text-muted-foreground">{t("write.to")}
            <input value={addr} onChange={e => setAddr(e.target.value)} placeholder={t("write.emailPlaceholder")} className={cn(field, !addr.includes("@") && "border-amber-500/50")} /></label>
          <label className="grid gap-1 text-xs text-muted-foreground">{t("write.subject")}
            <input value={subj} onChange={e => setSubj(e.target.value)} className={field} /></label>
          {canText && (
            <div className="grid gap-1 text-xs text-muted-foreground">{t("write.sendAs")}
              <div className="grid grid-cols-2 gap-1.5" role="radiogroup" aria-label={t("write.sendAs")}>
                {([["text", t("write.sendAsText")], ["pdf", t("write.sendAsPdf")]] as const).map(([v, label]) => (
                  <button key={v} type="button" role="radio" aria-checked={sendAs === v} onClick={() => chooseSendAs(v)}
                    className={cn("rounded-lg border px-2.5 py-1.5 text-sm transition",
                      sendAs === v ? "border-primary bg-primary/10 text-foreground" : "border-border hover:bg-muted")}>{label}</button>
                ))}
              </div>
            </div>
          )}
          {sendAs === "text" && canText ? (
            <p className="text-xs text-muted-foreground">{t("write.sendAsTextNote")}</p>
          ) : (
            <label className="grid gap-1 text-xs text-muted-foreground">{t("write.messageLabel")}
              <textarea value={message} onChange={e => setMessage(e.target.value)} rows={5} className={cn(field, "resize-y")} /></label>
          )}
          {finalNote(doc)}
          {error && <p className="text-sm text-red-400">{error}</p>}
          <div className="flex justify-end gap-2">
            <button onClick={onClose} className="rounded-lg px-3 py-1.5 text-sm hover:bg-muted">{t("common.cancel")}</button>
            <button onClick={send} disabled={busy || !accountId || !addr.includes("@")} className="rounded-lg bg-primary text-primary-foreground px-4 py-1.5 text-sm font-medium disabled:opacity-50 flex items-center gap-1.5">
              {busy && <Loader2 className="w-4 h-4 animate-spin" />} {t("write.send")}</button>
          </div>
        </>
      )}
    </Shell>
  );
}

const LEVELS = [
  { id: "private", label: "write.visPrivate" },
  { id: "parents", label: "write.visParents" },
  { id: "shared", label: "write.visShared" },
];

export function FileDialog({ doc, onClose, onDone }: { doc: WrittenDoc; onClose: () => void; onDone: (d: WrittenDoc) => void }) {
  const { t } = useTranslation();
  const [vis, setVis] = useState("private");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function file() {
    setBusy(true); setError("");
    try { onDone((await api.post<{ document: WrittenDoc }>(`/api/writing/${doc.id}/paperless`, { visibility: vis })).document); }
    catch (e: any) { setError(e?.message || String(e)); }
    finally { setBusy(false); }
  }
  return (
    <Shell title={t("write.fileTitle")} onClose={onClose}>
      <p className="text-sm text-muted-foreground">{t("write.fileBody")}</p>
      <div className="flex gap-1.5 flex-wrap" role="group" aria-label={t("write.visibility")}>
        {LEVELS.map(l => (
          <button key={l.id} onClick={() => setVis(l.id)} aria-pressed={vis === l.id}
                  className={cn("rounded-full px-3 py-1.5 text-sm border", vis === l.id ? "bg-primary text-primary-foreground border-primary" : "border-border hover:bg-muted")}>{t(l.label)}</button>
        ))}
      </div>
      {finalNote(doc)}
      {error && <p className="text-sm text-red-400">{error}</p>}
      <div className="flex justify-end gap-2">
        <button onClick={onClose} className="rounded-lg px-3 py-1.5 text-sm hover:bg-muted">{t("common.cancel")}</button>
        <button onClick={file} disabled={busy} className="rounded-lg bg-primary text-primary-foreground px-4 py-1.5 text-sm font-medium disabled:opacity-50 flex items-center gap-1.5">
          {busy && <Loader2 className="w-4 h-4 animate-spin" />} {t("write.file")}</button>
      </div>
    </Shell>
  );
}
