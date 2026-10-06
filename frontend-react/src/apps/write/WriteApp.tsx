/**
 * Schreiben (/write) — letters with your own letterhead. On the left your
 * drafts and what has left the house, in the middle the sheet you type
 * on, on the right the page as it will be printed (the same layout code
 * that makes the PDF). Yorik writes drafts from the chat (skill
 * `write_letter`); here they are corrected, turned into a PDF, sent or
 * filed in Paperless. A letter that was sent or filed is final: it stays
 * what its PDF says, "Als neuen Entwurf" makes a copy to go on from.
 *
 * Backend: /api/writing (backend/writing/routes.py). The look comes from
 * Settings → You → Your letterhead.
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { CheckCircle2, Copy, FileText, Loader2, PenLine, Plus, Receipt, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { formatDate } from "@/i18n/format";
import { api } from "@/lib/api";
import { Dock } from "@/components/Dock";
import { cn } from "@/lib/utils";
import { InvoiceEditor } from "./InvoiceEditor";
import { LetterEditor } from "./LetterEditor";
import type { WrittenDoc, WrittenDocRow } from "./types";

const day = (s: string) => formatDate(new Date(`${s.slice(0, 10)}T00:00:00`), { day: "2-digit", month: "2-digit", year: "numeric" });

export function WriteApp() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [rows, setRows] = useState<WrittenDocRow[] | null>(null);
  const [kinds, setKinds] = useState<string[]>(["letter"]);
  const [menu, setMenu] = useState(false);
  const [doc, setDoc] = useState<WrittenDoc | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; bad?: boolean } | null>(null);
  const [sure, setSure] = useState(false);
  const openId = Number(params.get("id")) || null;

  const say = useCallback((text: string, bad = false) => { setNote({ text, bad }); window.setTimeout(() => setNote(n => (n && n.text === text ? null : n)), 5000); }, []);
  const loadList = useCallback(async () => {
    try { const r = await api.get<{ documents: WrittenDocRow[]; kinds: string[] }>("/api/writing"); setRows(r.documents); setKinds(r.kinds); } catch { setRows([]); }
  }, []);
  useEffect(() => { loadList(); }, [loadList]);

  useEffect(() => {
    setSure(false);
    if (!openId) { setDoc(null); return; }
    let gone = false;
    api.get<WrittenDoc>(`/api/writing/${openId}`).then(d => { if (!gone) setDoc(d); }).catch(() => { if (!gone) { setDoc(null); say(t("write.notFound"), true); } });
    return () => { gone = true; };
  }, [openId, say, t]);

  const open = (id: number | null) => setParams(id ? { id: String(id) } : {}, { replace: false });

  async function create(kind: string) {
    setBusy(true); setMenu(false);
    try { const d = await api.post<WrittenDoc>("/api/writing", { kind, content: kind === "letter" ? { add_closing: true } : {} }); await loadList(); open(d.id); }
    catch (e: any) { say(t("write.createFailed", { error: e?.message || e }), true); }
    finally { setBusy(false); }
  }
  async function duplicate() {
    if (!doc) return;
    try { const d = await api.post<WrittenDoc>(`/api/writing/${doc.id}/duplicate`, {}); await loadList(); open(d.id); say(t("write.copyCreated")); }
    catch (e: any) { say(t("write.copyFailed", { error: e?.message || e }), true); }
  }
  async function remove() {
    if (!doc) return;
    if (!sure) { setSure(true); return; }
    try { await api.delete(`/api/writing/${doc.id}`); open(null); await loadList(); }
    catch (e: any) { say(t("write.deleteFailed", { error: e?.message || e }), true); }
  }

  const drafts = (rows || []).filter(r => r.status === "draft"), finals = (rows || []).filter(r => r.status === "final");
  const Row = ({ r }: { r: WrittenDocRow }) => (
    <button onClick={() => open(r.id)} className={cn("w-full text-left rounded-lg px-3 py-2 transition", openId === r.id ? "bg-primary/15 ring-1 ring-primary/40" : "hover:bg-muted")}>
      <div className="flex items-center gap-2 text-sm font-medium truncate">
        {r.status === "final" ? <CheckCircle2 className="w-3.5 h-3.5 shrink-0 text-emerald-500" /> : r.kind === "letter" ? <PenLine className="w-3.5 h-3.5 shrink-0 text-muted-foreground" /> : <Receipt className="w-3.5 h-3.5 shrink-0 text-muted-foreground" />}
        <span className="truncate">{r.kind === "letter" ? (r.title || t("write.noSubject")) : `${r.kind === "quote" ? t("write.quote") : t("write.invoice")} ${r.number || ""}${r.title ? ` · ${r.title}` : ""}`}</span>
      </div>
      <div className="text-xs text-muted-foreground truncate pl-5">{r.recipient?.name || (r.kind === "letter" ? t("write.noRecipient") : t("write.noCustomer"))} · {day(r.finalised_at || r.updated_at)}</div>
    </button>
  );

  return (
    <div className="h-screen flex flex-col bg-background text-foreground">
      <div className="flex-1 min-h-0 flex flex-col md:flex-row pb-24 md:pb-28">
        {/* list: a column on the desktop; on the phone it gives way to the open letter */}
        <aside className={cn("md:w-72 shrink-0 md:border-r border-border flex flex-col min-h-0", openId ? "hidden md:flex" : "flex flex-1 md:flex-none")}>
          <div className="p-4 max-md:pr-14 flex items-center justify-between gap-2">
            <h1 className="text-lg font-semibold flex items-center gap-2"><FileText className="w-5 h-5 text-primary" /> {t("write.appTitle")}</h1>
            <div className="relative">
              <button onClick={() => (kinds.length > 1 ? setMenu(m => !m) : create("letter"))} disabled={busy} aria-haspopup={kinds.length > 1} aria-expanded={menu}
                      className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium disabled:opacity-50">
                {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} {kinds.length > 1 ? t("write.new") : t("write.letter")}
              </button>
              {menu && (
                <div className="absolute right-0 top-full mt-1 z-30 w-44 rounded-lg border border-border bg-card shadow-xl overflow-hidden" role="menu">
                  {[{ k: "letter", l: t("write.letter"), I: PenLine }, { k: "invoice", l: t("write.invoice"), I: Receipt }, { k: "quote", l: t("write.quote"), I: FileText }].filter(o => kinds.includes(o.k)).map(o => (
                    <button key={o.k} role="menuitem" onClick={() => create(o.k)} className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left hover:bg-muted"><o.I className="w-4 h-4 text-muted-foreground" /> {o.l}</button>
                  ))}
                </div>
              )}
            </div>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-4 space-y-4">
            {rows === null && <div className="p-4"><Loader2 className="w-5 h-5 animate-spin text-muted-foreground" /></div>}
            {rows && rows.length === 0 && (
              <p className="px-3 text-sm text-muted-foreground">{t("write.emptyList")}</p>
            )}
            {drafts.length > 0 && <section><h2 className="px-3 mb-1 text-xs font-semibold text-muted-foreground">{t("write.drafts")}</h2>{drafts.map(r => <Row key={r.id} r={r} />)}</section>}
            {finals.length > 0 && <section><h2 className="px-3 mb-1 text-xs font-semibold text-muted-foreground">{t("write.finished")}</h2>{finals.map(r => <Row key={r.id} r={r} />)}</section>}
          </div>
          <button onClick={() => navigate("/settings?tab=letters")} className="m-3 rounded-lg border border-border px-3 py-2 text-xs text-muted-foreground hover:bg-muted text-left">
            {t("write.changeLook")}
          </button>
        </aside>

        <main className={cn("flex-1 min-w-0 min-h-0 flex-col", openId ? "flex" : "hidden md:flex")}>
          {!doc && (
            <div className="flex-1 grid place-items-center text-sm text-muted-foreground p-8 text-center">
              {openId ? <Loader2 className="w-6 h-6 animate-spin" /> : t("write.pickOne")}
            </div>
          )}
          {doc && doc.kind === "letter" && (
            <LetterEditor key={doc.id} doc={doc} say={say} onBack={() => open(null)}
                          onChanged={d => { setDoc(d); setRows(rs => rs && rs.map(r => r.id === d.id ? { ...r, title: d.title, recipient: d.recipient, status: d.status, number: d.number, updated_at: d.updated_at, finalised_at: d.finalised_at } : r)); }}
                          extra={doc.status === "final"
                            ? <button onClick={duplicate} className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted"><Copy className="w-4 h-4" /> {t("write.asNewDraft")}</button>
                            : <button onClick={remove} onBlur={() => setSure(false)} className={cn("flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm", sure ? "bg-red-500 text-white" : "text-muted-foreground hover:bg-muted")}><Trash2 className="w-4 h-4" /> {sure ? t("write.reallyDelete") : t("common.delete")}</button>} />
          )}
          {doc && doc.kind !== "letter" && (
            <InvoiceEditor key={doc.id} doc={doc} say={say} onBack={() => open(null)} onOpen={async id => { await loadList(); open(id); }}
                          onChanged={d => { setDoc(d); setRows(rs => rs && rs.map(r => r.id === d.id ? { ...r, title: d.title, recipient: d.recipient, status: d.status, number: d.number, updated_at: d.updated_at, finalised_at: d.finalised_at } : r)); }}
                          extra={doc.status === "final"
                            ? <button onClick={duplicate} className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted"><Copy className="w-4 h-4" /> {t("write.asNewDraft")}</button>
                            : <button onClick={remove} onBlur={() => setSure(false)} className={cn("flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm", sure ? "bg-red-500 text-white" : "text-muted-foreground hover:bg-muted")}><Trash2 className="w-4 h-4" /> {sure ? t("write.reallyDelete") : t("common.delete")}</button>} />
          )}
        </main>
      </div>
      {note && (
        <div role="status" className={cn("fixed left-1/2 -translate-x-1/2 bottom-28 z-40 rounded-full px-4 py-2 text-sm shadow-lg border", note.bad ? "bg-red-500/15 border-red-500/40 text-red-200" : "bg-card border-border")}>{note.text}</div>
      )}
      <Dock activeAppId="write" />
    </div>
  );
}
