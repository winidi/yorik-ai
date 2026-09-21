/**
 * InvoiceEditor — an invoice or a quote: the customer, what it is about,
 * the line items as a table you type into, the dates. The sums are the
 * server's (the same arithmetic that feeds the PDF and the e-invoice);
 * nothing is computed here. Beside it: what is still missing, the number
 * it would get, the page as it will be printed.
 *
 * "Fertigstellen" is a step of its own: only then the number is taken,
 * the PDF made and, for an invoice in Germany, the e-invoice built and
 * checked. If that cannot be done the person is told and decides; no
 * number is lost on a failure. A final document is shown, not edited.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, Check, Eye, FileDown, FolderInput, Loader2, Plus, Receipt, Send, ShieldCheck, Stamp, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { FileDialog, SendDialog } from "./LetterDialogs";
import { PagePreview } from "./PagePreview";
import { RecipientFields, recipientOf, recipientPayload, type RecipientValue } from "./RecipientFields";
import type { DocState, InvoiceLine, WrittenDoc } from "./types";

const EMPTY: InvoiceLine = { text: "", qty: "1", unit: "", unit_price: "" };
const plain = (html?: string) => (html || "").replace(/<\/p>\s*<p>/g, "\n\n").replace(/<br\s*\/?>/g, "\n").replace(/<[^>]+>/g, "")
  .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").trim();

export function InvoiceEditor({ doc, onChanged, onBack, onOpen, say, extra }: {
  doc: WrittenDoc; onChanged: (d: WrittenDoc) => void; onBack: () => void; onOpen: (id: number) => void;
  say: (text: string, bad?: boolean) => void; extra?: React.ReactNode;
}) {
  const final = doc.status === "final";
  const quote = doc.kind === "quote";
  const word = quote ? "Angebot" : "Rechnung";
  const [to, setTo] = useState<RecipientValue>(() => recipientOf(doc));
  const [subject, setSubject] = useState(doc.content.subject || "");
  const [intro, setIntro] = useState(() => plain(doc.content.intro_html));
  const [closing, setClosing] = useState(() => plain(doc.content.closing_html));
  const [lines, setLines] = useState<InvoiceLine[]>(() => (doc.content.lines?.length ? doc.content.lines : [{ ...EMPTY }]));
  const [dates, setDates] = useState({ service_from: doc.content.service_from || "", service_to: doc.content.service_to || "", valid_until: doc.content.valid_until || "", customer_no: doc.content.customer_no || "" });
  const [saving, setSaving] = useState<"idle" | "dirty" | "saving" | "saved" | "failed">("idle");
  const [state, setState] = useState<DocState | null>(null);
  const [html, setHtml] = useState("");
  const [showPreview, setShowPreview] = useState(false);
  const [dialog, setDialog] = useState<"send" | "file" | "finalise" | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<{ message: string; problems?: string[]; canContinue?: boolean } | null>(null);
  const timer = useRef<number | null>(null);
  const latest = useRef({ to, subject, intro, closing, lines, dates });
  latest.current = { to, subject, intro, closing, lines, dates };

  const refresh = useCallback(async () => {
    try {
      const [st, pv] = await Promise.all([api.get<DocState>(`/api/writing/${doc.id}/state`), api.get<{ html: string }>(`/api/writing/${doc.id}/preview`)]);
      setState(st); setHtml(pv.html);
    } catch {}
  }, [doc.id]);
  useEffect(() => { refresh(); }, [refresh, doc.status]);

  const save = useCallback(async () => {
    if (final) return;
    const v = latest.current;
    setSaving("saving");
    try {
      const out = await api.patch<WrittenDoc>(`/api/writing/${doc.id}`, {
        title: v.subject, recipient: recipientPayload(v.to),
        content: { subject: v.subject, intro: v.intro, closing: v.closing, lines: v.lines.filter(l => l.text.trim() || l.unit_price.trim()), ...v.dates },
      });
      onChanged(out); setSaving("saved"); void refresh();
    } catch (e: any) { setSaving("failed"); say(`Speichern hat nicht geklappt: ${e?.message || e}`, true); }
  }, [doc.id, final, onChanged, refresh, say]);
  const saveRef = useRef(save); saveRef.current = save;
  function touch() {
    if (final) return;
    setSaving("dirty");
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { timer.current = null; void saveRef.current(); }, 900);
  }
  useEffect(() => () => { if (timer.current) { window.clearTimeout(timer.current); void saveRef.current(); } }, []);
  async function flush() { if (timer.current) { window.clearTimeout(timer.current); timer.current = null; await saveRef.current(); } }

  const setLine = (i: number, part: Partial<InvoiceLine>) => { setLines(ls => ls.map((l, j) => j === i ? { ...l, ...part } : l)); touch(); };

  async function finalise(withoutE = false) {
    setBusy(true); setProblem(null);
    try {
      const out = await api.post<{ document: WrittenDoc }>(`/api/writing/${doc.id}/finalise`, { without_e_invoice: withoutE });
      setDialog(null); onChanged(out.document);
      say(`${word} ${out.document.number} ist fertig.`);
    } catch (e: any) {
      const d = e?.body?.detail;
      setProblem(typeof d === "object" && d ? { message: d.message || "Das hat nicht geklappt.", problems: d.problems || d.missing, canContinue: !!d.can_continue_without } : { message: e?.message || String(e) });
    } finally { setBusy(false); }
  }

  async function toInvoice() {
    try { const d = await api.post<WrittenDoc>(`/api/writing/${doc.id}/to-invoice`, {}); say("Rechnung als Entwurf angelegt, mit den Positionen aus dem Angebot."); onOpen(d.id); }
    catch (e: any) { say(`Umwandeln hat nicht geklappt: ${e?.message || e}`, true); }
  }

  // the server's line totals are for the lines that were saved (empty ones are left out)
  const totalOf = (i: number) => { const kept = lines.slice(0, i + 1).filter(l => l.text.trim() || l.unit_price.trim()); return lines[i].text.trim() || lines[i].unit_price.trim() ? state?.totals?.lines[kept.length - 1] || "" : ""; };
  const missing = state?.missing || [];
  const e = state?.e_invoice;
  const input = "rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary disabled:opacity-70";
  const cell = "w-full bg-transparent px-2 py-1.5 text-sm text-zinc-900 placeholder:text-zinc-400 outline-none focus:bg-violet-50 disabled:text-zinc-700";
  const leaveNote = "Erst fertigstellen: die Nummer wird dabei vergeben";

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      {/* room on the right: the notification bell floats over the top corner */}
      <header className="flex items-center gap-2 flex-wrap pl-4 pr-16 py-3 border-b border-border">
        <button onClick={onBack} className="md:hidden p-1.5 rounded-md hover:bg-muted" aria-label="Zur Liste"><ArrowLeft className="w-5 h-5" /></button>
        <div className="min-w-0 mr-auto">
          <div className="font-semibold truncate flex items-center gap-2"><Receipt className="w-4 h-4 text-primary shrink-0" /> {word} {doc.number || ""} {subject && <span className="font-normal text-muted-foreground truncate">· {subject}</span>}</div>
          <div className="text-[11px] text-muted-foreground flex items-center gap-1.5 flex-wrap">
            {final ? <><Check className="w-3 h-3 text-emerald-500" /> Fertig seit {(doc.finalised_at || "").slice(0, 10).split("-").reverse().join(".")} · wird nicht mehr geändert</>
              : saving === "saving" ? <><Loader2 className="w-3 h-3 animate-spin" /> speichert …</>
              : saving === "dirty" ? "ungespeichert …" : saving === "failed" ? <span className="text-red-400">nicht gespeichert</span>
              : <>Entwurf · gespeichert{state?.next_number ? ` · bekommt die Nummer ${state.next_number}` : ""}</>}
            {final && !quote && (doc.content.e_invoice?.checked
              ? <span className="rounded-full bg-emerald-500/15 text-emerald-300 px-2 py-0.5 flex items-center gap-1"><ShieldCheck className="w-3 h-3" /> E-Rechnung, geprüft</span>
              : <span className="rounded-full bg-amber-500/15 text-amber-300 px-2 py-0.5">einfaches PDF, keine E-Rechnung</span>)}
          </div>
        </div>
        <button onClick={() => setShowPreview(v => !v)} className={cn("xl:hidden flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted", showPreview && "bg-muted")}><Eye className="w-4 h-4" /> Vorschau</button>
        <a href={`/api/writing/${doc.id}/pdf`} target="_blank" rel="noreferrer" onClick={() => { void flush(); }}
           className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted"><FileDown className="w-4 h-4" /> {final ? "PDF" : "PDF-Entwurf"}</a>
        {final ? (
          <>
            <button onClick={() => setDialog("file")} className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted"><FolderInput className="w-4 h-4" /> Ablegen</button>
            <button onClick={() => setDialog("send")} className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium"><Send className="w-4 h-4" /> Senden</button>
            {quote && <button onClick={toInvoice} className="flex items-center gap-1.5 rounded-lg border border-primary/50 text-primary px-3 py-1.5 text-sm hover:bg-primary/10"><Receipt className="w-4 h-4" /> In Rechnung umwandeln</button>}
          </>
        ) : (
          <button onClick={async () => { await flush(); setProblem(null); setDialog("finalise"); }} disabled={missing.length > 0 || !state} title={missing.length ? `Fehlt noch: ${missing.join(", ")}` : leaveNote}
                  className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium disabled:opacity-50"><Stamp className="w-4 h-4" /> Fertigstellen</button>
        )}
        {extra}
      </header>

      <div className="flex-1 min-h-0 grid xl:grid-cols-[minmax(0,1fr)_minmax(0,0.8fr)]">
        <div className={cn("min-h-0 overflow-y-auto p-4 md:p-6", showPreview && "hidden xl:block")}>
          <div className="mx-auto max-w-[760px] grid gap-4">
            {!final && missing.length > 0 && (
              <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm">
                <div className="font-medium text-amber-200 flex items-center gap-2"><AlertTriangle className="w-4 h-4" /> Fehlt noch, bevor die {word} fertig werden kann</div>
                <ul className="mt-1 list-disc pl-6 text-amber-100/90">{missing.map(m => <li key={m}>{m}</li>)}</ul>
              </div>
            )}
            <RecipientFields docId={doc.id} value={to} disabled={final} label="Kunde" say={say} onChange={v => { setTo(v); touch(); }}
                             onPicked={d => { setTo(recipientOf(d)); onChanged(d); void refresh(); }} />

            <section className="grid gap-2 sm:grid-cols-4">
              <label className="grid gap-1 text-[11px] text-muted-foreground sm:col-span-2">Betreff
                <input disabled={final} value={subject} onChange={ev => { setSubject(ev.target.value); touch(); }} placeholder="Worum geht es?" className={input} /></label>
              <label className="grid gap-1 text-[11px] text-muted-foreground">Kundennummer
                <input disabled={final} value={dates.customer_no} onChange={ev => { setDates(d => ({ ...d, customer_no: ev.target.value })); touch(); }} placeholder="optional" className={input} /></label>
              {quote ? (
                <label className="grid gap-1 text-[11px] text-muted-foreground">Gültig bis
                  <input type="date" disabled={final} value={dates.valid_until} onChange={ev => { setDates(d => ({ ...d, valid_until: ev.target.value })); touch(); }} className={input} /></label>
              ) : <span />}
              {!quote && (
                <>
                  <label className="grid gap-1 text-[11px] text-muted-foreground sm:col-span-2">Leistung vom
                    <input type="date" disabled={final} value={dates.service_from} onChange={ev => { setDates(d => ({ ...d, service_from: ev.target.value })); touch(); }}
                           className={cn(input, !final && !dates.service_from && !dates.service_to && "border-amber-500/50")} /></label>
                  <label className="grid gap-1 text-[11px] text-muted-foreground sm:col-span-2">bis (leer = ein Tag)
                    <input type="date" disabled={final} value={dates.service_to} onChange={ev => { setDates(d => ({ ...d, service_to: ev.target.value })); touch(); }} className={input} /></label>
                </>
              )}
            </section>

            <div className="rounded-xl bg-white shadow-lg ring-1 ring-black/10 p-4 md:p-6 text-zinc-900">
              <textarea disabled={final} value={intro} onChange={ev => { setIntro(ev.target.value); touch(); }} rows={2} placeholder={quote ? "Einleitung, z. B. „gern bieten wir Ihnen an:“" : "Einleitung, z. B. „vielen Dank für Ihren Auftrag. Wir berechnen:“"}
                        className="w-full resize-y bg-transparent text-[15px] placeholder:text-zinc-400 outline-none mb-3" />
              <div className="overflow-x-auto -mx-1 px-1">
                <table className="w-full min-w-[560px] border-collapse">
                  <thead><tr className="text-[11px] uppercase tracking-wide text-zinc-500 text-left">
                    <th className="py-1.5 px-2 w-8">Pos.</th><th className="py-1.5 px-2">Beschreibung</th><th className="py-1.5 px-2 w-20 text-right">Menge</th>
                    <th className="py-1.5 px-2 w-24">Einheit</th><th className="py-1.5 px-2 w-28 text-right">Einzelpreis €</th>
                    {!state?.totals?.small_business && <th className="py-1.5 px-2 w-16 text-right">USt %</th>}<th className="py-1.5 px-2 w-28 text-right">Gesamt</th><th className="w-8" />
                  </tr></thead>
                  <tbody>
                    {lines.map((l, i) => (
                      <tr key={i} className="border-t border-zinc-200 align-top">
                        <td className="py-2 px-2 text-sm text-zinc-500">{i + 1}</td>
                        <td><textarea disabled={final} value={l.text} onChange={ev => setLine(i, { text: ev.target.value })} rows={Math.max(1, l.text.split("\n").length)} placeholder="Was wurde geleistet?" aria-label={`Position ${i + 1}, Beschreibung`} className={cn(cell, "resize-none")} /></td>
                        <td><input disabled={final} value={l.qty} onChange={ev => setLine(i, { qty: ev.target.value })} inputMode="decimal" aria-label={`Position ${i + 1}, Menge`} className={cn(cell, "text-right tabular-nums")} /></td>
                        <td><input disabled={final} value={l.unit} onChange={ev => setLine(i, { unit: ev.target.value })} list="w-units" placeholder="Std." aria-label={`Position ${i + 1}, Einheit`} className={cell} /></td>
                        <td><input disabled={final} value={l.unit_price} onChange={ev => setLine(i, { unit_price: ev.target.value })} inputMode="decimal" placeholder="0,00" aria-label={`Position ${i + 1}, Einzelpreis`} className={cn(cell, "text-right tabular-nums")} /></td>
                        {!state?.totals?.small_business && <td><input disabled={final} value={l.vat_percent || ""} onChange={ev => setLine(i, { vat_percent: ev.target.value })} inputMode="decimal" placeholder="19" aria-label={`Position ${i + 1}, Umsatzsteuer`} className={cn(cell, "text-right tabular-nums")} /></td>}
                        <td className="py-2 px-2 text-sm text-right tabular-nums whitespace-nowrap">{saving === "dirty" || saving === "saving" ? "…" : totalOf(i)}</td>
                        <td>{!final && lines.length > 1 && <button onClick={() => { setLines(ls => ls.filter((_, j) => j !== i)); touch(); }} aria-label={`Position ${i + 1} entfernen`} className="p-1.5 text-zinc-400 hover:text-red-500"><Trash2 className="w-4 h-4" /></button>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <datalist id="w-units"><option value="Std." /><option value="Stk." /><option value="pauschal" /><option value="Tag" /><option value="km" /><option value="m²" /></datalist>
              </div>
              {!final && <button onClick={() => { setLines(ls => [...ls, { ...EMPTY }]); }} className="mt-2 flex items-center gap-1.5 rounded-lg px-2 py-1 text-sm text-violet-700 hover:bg-violet-50"><Plus className="w-4 h-4" /> Position</button>}
              {state?.totals && (
                <table className="ml-auto mt-3 text-sm tabular-nums">
                  <tbody>
                    <tr><td className="pr-6 py-0.5 text-zinc-600">Nettobetrag</td><td className="text-right">{state.totals.net}</td></tr>
                    {state.totals.vat_rows.map(v => <tr key={v.rate}><td className="pr-6 py-0.5 text-zinc-600">zzgl. {v.rate} % USt.</td><td className="text-right">{v.vat}</td></tr>)}
                    <tr className="font-bold text-[15px]"><td className="pr-6 pt-1.5 border-t border-zinc-300">{quote ? "Angebotssumme" : "Rechnungsbetrag"}</td><td className="text-right pt-1.5 border-t border-zinc-300">{state.totals.gross}</td></tr>
                  </tbody>
                </table>
              )}
              {state?.totals?.small_business && <p className="mt-2 text-xs text-zinc-500">Kleinunternehmer (§ 19 UStG): keine Umsatzsteuer. Einstellbar im Briefpapier.</p>}
              <textarea disabled={final} value={closing} onChange={ev => { setClosing(ev.target.value); touch(); }} rows={2} placeholder="Schlusstext (optional). Der Zahlungssatz mit Fälligkeit kommt aus dem Briefpapier."
                        className="w-full resize-y bg-transparent text-[15px] placeholder:text-zinc-400 outline-none mt-4" />
            </div>

            {!final && !quote && e?.wanted && (
              <p className={cn("text-xs flex items-start gap-2", e.available ? "text-muted-foreground" : "text-amber-300")}>
                <ShieldCheck className="w-4 h-4 shrink-0 mt-px" />
                {e.available ? "Wird beim Fertigstellen eine E-Rechnung (ZUGFeRD / Factur-X): das PDF trägt die Rechnungsdaten maschinenlesbar in sich und wird vorher geprüft."
                  : "E-Rechnung ist hier noch nicht möglich: die Erweiterung „ZUGFeRD“ ist nicht installiert (Settings → Extensions → ZUGFeRD → Install). Ohne sie entsteht ein einfaches PDF; du wirst beim Fertigstellen gefragt."}
              </p>
            )}
          </div>
        </div>
        <div className={cn("min-h-0 overflow-y-auto border-l border-border bg-muted/30 p-4", showPreview ? "block" : "hidden xl:block")}>
          <PagePreview html={html} />
        </div>
      </div>

      {dialog === "finalise" && (
        <div className="fixed inset-0 z-50 grid place-items-center p-4 bg-black/60" onClick={() => !busy && setDialog(null)}>
          <div role="dialog" aria-modal="true" aria-label={`${word} fertigstellen`} onClick={ev => ev.stopPropagation()} className="w-full max-w-md rounded-2xl bg-card border border-border shadow-2xl p-5 grid gap-3">
            <h2 className="font-semibold flex items-center gap-2"><Stamp className="w-4 h-4 text-primary" /> {word} fertigstellen</h2>
            {!problem ? (
              <p className="text-sm text-muted-foreground">Die {word} bekommt die Nummer <b className="text-foreground">{state?.next_number || "…"}</b> und das heutige Datum, wird als PDF festgehalten und danach nicht mehr geändert.
                {!quote && e?.wanted && e.available ? " Sie wird als geprüfte E-Rechnung erzeugt." : ""} Eine Nummer wird nur vergeben, wenn alles geklappt hat.</p>
            ) : (
              <div className="text-sm grid gap-2">
                <p className="text-amber-200">{problem.message}</p>
                {problem.problems && problem.problems.length > 0 && <ul className="list-disc pl-5 text-xs text-muted-foreground">{problem.problems.map(p => <li key={p}>{p}</li>)}</ul>}
                {problem.canContinue && <p className="text-xs text-muted-foreground">Du kannst sie als einfaches PDF fertigstellen. Für Geschäftskunden in Deutschland ist die E-Rechnung ab 2027/2028 Pflicht; Privatkunden brauchen sie nicht.</p>}
              </div>
            )}
            <div className="flex justify-end gap-2 flex-wrap">
              <button onClick={() => setDialog(null)} disabled={busy} className="rounded-lg px-3 py-1.5 text-sm hover:bg-muted">Abbrechen</button>
              {problem?.canContinue && <button onClick={() => finalise(true)} disabled={busy} className="rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted flex items-center gap-1.5">{busy && <Loader2 className="w-4 h-4 animate-spin" />} Als einfaches PDF fertigstellen</button>}
              {!problem && <button onClick={() => finalise(false)} disabled={busy} className="rounded-lg bg-primary text-primary-foreground px-4 py-1.5 text-sm font-medium disabled:opacity-50 flex items-center gap-1.5">{busy && <Loader2 className="w-4 h-4 animate-spin" />} Fertigstellen</button>}
            </div>
          </div>
        </div>
      )}
      {dialog === "send" && <SendDialog doc={doc} to={to.email} subject={`${word} ${doc.number || ""}${subject ? ` · ${subject}` : ""}`} onClose={() => setDialog(null)} onDone={d => { setDialog(null); onChanged(d); say("Verschickt."); }} />}
      {dialog === "file" && <FileDialog doc={doc} onClose={() => setDialog(null)} onDone={d => { setDialog(null); onChanged(d); say("In Paperless abgelegt."); }} />}
    </div>
  );
}
