/**
 * LetterEditor — one letter: who it goes to, the subject, the text on a
 * sheet (TipTap), beside it the page as it will be printed. Everything
 * typed is saved a moment later; the preview follows the saved state.
 * A final letter is shown, not edited.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { Underline } from "@tiptap/extension-underline";
import { Placeholder } from "@tiptap/extension-placeholder";
import { ArrowLeft, Bold, Check, Eye, FileDown, FolderInput, Italic, List, ListOrdered, Loader2, Redo2, Search, Send, Sparkles, Underline as UnderlineIcon, Undo2, X } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { FileDialog, SendDialog } from "./LetterDialogs";
import type { Recipient, WrittenDoc } from "./types";

const SHEET_PX = 794 + 60;
type ContactHit = { id: number; display_name: string; legal_name?: string | null; kind?: string };

export function LetterEditor({ doc, onChanged, onBack, say, extra }: {
  doc: WrittenDoc; onChanged: (d: WrittenDoc) => void; onBack: () => void;
  say: (text: string, bad?: boolean) => void; extra?: React.ReactNode;
}) {
  const final = doc.status === "final";
  const [name, setName] = useState(doc.recipient?.name || "");
  const [address, setAddress] = useState((doc.recipient?.address_lines || []).join("\n"));
  const [email, setEmail] = useState(doc.recipient?.email || "");
  const [contactId, setContactId] = useState<number | null>(doc.recipient?.contact_id ?? null);
  const [subject, setSubject] = useState(doc.content.subject || "");
  const [closing, setClosing] = useState(!!doc.content.add_closing);
  const [saving, setSaving] = useState<"idle" | "dirty" | "saving" | "saved" | "failed">("idle");
  const [html, setHtml] = useState("");
  const [showPreview, setShowPreview] = useState(false);          // phone: the preview takes the editor's place
  const [dialog, setDialog] = useState<"send" | "file" | null>(null);
  const [hits, setHits] = useState<ContactHit[] | null>(null);
  const [wish, setWish] = useState("");
  const [rewriting, setRewriting] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(0.5);
  const timer = useRef<number | null>(null);
  const latest = useRef({ name, address, email, contactId, subject, closing });
  latest.current = { name, address, email, contactId, subject, closing };

  const editor = useEditor({
    extensions: [StarterKit, Underline, Placeholder.configure({ placeholder: "Sehr geehrte Damen und Herren, …" })],
    content: doc.content.text_html || "",
    editable: !final,
    editorProps: { attributes: { class: "outline-none min-h-[22rem] text-[15px] leading-relaxed text-zinc-900 [&_p]:mb-3 [&_ul]:list-disc [&_ul]:pl-6 [&_ol]:list-decimal [&_ol]:pl-6 [&_h2]:text-lg [&_h2]:font-semibold [&_h2]:mt-4 [&_h2]:mb-2" } },
    onUpdate: () => touch(),
  });

  useEffect(() => { editor?.setEditable(!final); }, [editor, final]);

  const loadPreview = useCallback(async () => {
    try { setHtml((await api.get<{ html: string }>(`/api/writing/${doc.id}/preview`)).html); } catch {}
  }, [doc.id]);
  useEffect(() => { loadPreview(); }, [loadPreview]);

  const save = useCallback(async () => {
    if (final || !editor) return;
    const v = latest.current;
    setSaving("saving");
    try {
      const out = await api.patch<WrittenDoc>(`/api/writing/${doc.id}`, {
        recipient: { contact_id: v.contactId, name: v.name, address_lines: v.address.split("\n"), email: v.email },
        content: { ...doc.content, subject: v.subject, text_html: editor.getHTML(), add_closing: v.closing },
      });
      onChanged(out); setSaving("saved"); void loadPreview();
    } catch (e: any) { setSaving("failed"); say(`Speichern hat nicht geklappt: ${e?.message || e}`, true); }
  }, [doc.id, doc.content, editor, final, loadPreview, onChanged, say]);
  const saveRef = useRef(save); saveRef.current = save;

  // everything typed is saved a moment later, and at the latest when the editor goes away
  function touch() {
    if (final) return;
    setSaving("dirty");
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { timer.current = null; void saveRef.current(); }, 900);
  }
  useEffect(() => () => { if (timer.current) { window.clearTimeout(timer.current); void saveRef.current(); } }, []);
  async function flush() { if (timer.current) { window.clearTimeout(timer.current); timer.current = null; await saveRef.current(); } }

  useLayoutEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const fit = () => { if (el.clientWidth) setScale(Math.min(1, el.clientWidth / SHEET_PX)); };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [showPreview]);

  // recipient from the contacts: type a name, pick a hit, the address comes along
  useEffect(() => {
    if (final || contactId || name.trim().length < 2) { setHits(null); return; }
    const t = window.setTimeout(async () => {
      try { setHits((await api.get<ContactHit[]>(`/api/contacts?q=${encodeURIComponent(name.trim())}&limit=6`)).slice(0, 6)); } catch { setHits(null); }
    }, 300);
    return () => window.clearTimeout(t);
  }, [name, contactId, final]);

  async function pick(c: ContactHit) {
    setHits(null);
    try {
      const out = await api.patch<WrittenDoc>(`/api/writing/${doc.id}`, { contact_id: c.id });
      const r = out.recipient as Recipient;
      setName(r.name || ""); setAddress((r.address_lines || []).join("\n")); setEmail(r.email || ""); setContactId(r.contact_id ?? null);
      onChanged(out); void loadPreview();
      if (!r.address_lines?.length) say("Für diesen Kontakt ist keine Adresse hinterlegt. Du kannst sie hier eintragen.");
    } catch (e: any) { say(`Kontakt übernehmen hat nicht geklappt: ${e?.message || e}`, true); }
  }

  async function rewrite() {
    if (!editor || !wish.trim()) return;
    const { from, to, empty } = editor.state.selection;
    const text = empty ? editor.getText({ blockSeparator: "\n\n" }) : editor.state.doc.textBetween(from, to, "\n\n");
    if (!text.trim()) { say("Es gibt noch keinen Text zum Überarbeiten."); return; }
    setRewriting(true);
    try {
      const out = (await api.post<{ text: string }>("/api/writing/rewrite", { text, instruction: wish })).text;
      const paragraphs = out.split(/\n{2,}/).map(p => ({ type: "paragraph", content: p.trim() ? p.trim().split("\n").flatMap((line, i) => [...(i ? [{ type: "hardBreak" }] : []), { type: "text", text: line }]).filter(n => n.type !== "text" || (n as any).text) : [] }));
      // replaced as an ordinary edit (not setContent), so that undo brings the old text back
      if (empty) editor.chain().focus().insertContentAt({ from: 0, to: editor.state.doc.content.size }, paragraphs).run();
      else editor.chain().focus().insertContentAt({ from, to }, paragraphs.length === 1 ? (paragraphs[0].content as any) : paragraphs).run();
      setWish(""); touch();
      say("Überarbeitet. Mit Rückgängig (↶) bekommst du den alten Text zurück.");
    } catch (e: any) { say(`Yorik konnte das nicht überarbeiten: ${e?.message || e}`, true); }
    finally { setRewriting(false); }
  }

  const missing = [!name.trim() && "Empfänger", !address.trim() && "Adresse"].filter(Boolean) as string[];
  const Tool = ({ on, active, label, children }: { on: () => void; active?: boolean; label: string; children: React.ReactNode }) => (
    <button type="button" onMouseDown={e => e.preventDefault()} onClick={on} title={label} aria-label={label} aria-pressed={active}
            className={cn("p-1.5 rounded-md", active ? "bg-primary/20 text-primary" : "text-muted-foreground hover:bg-muted")}>{children}</button>
  );
  const input = "rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary disabled:opacity-70";

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <header className="flex items-center gap-2 flex-wrap px-4 py-3 border-b border-border">
        <button onClick={onBack} className="md:hidden p-1.5 rounded-md hover:bg-muted" aria-label="Zur Liste"><ArrowLeft className="w-5 h-5" /></button>
        <div className="min-w-0 mr-auto">
          <div className="font-semibold truncate">{subject || "Ohne Betreff"}</div>
          <div className="text-[11px] text-muted-foreground flex items-center gap-1.5">
            {final ? <><Check className="w-3 h-3 text-emerald-500" /> Fertig seit {(doc.finalised_at || "").slice(0, 10).split("-").reverse().join(".")} · wird nicht mehr geändert</>
              : saving === "saving" ? <><Loader2 className="w-3 h-3 animate-spin" /> speichert …</>
              : saving === "dirty" ? "ungespeichert …" : saving === "failed" ? <span className="text-red-400">nicht gespeichert</span> : "Entwurf · gespeichert"}
            {!final && missing.length > 0 && <span className="rounded-full bg-amber-500/15 text-amber-300 px-2 py-0.5">fehlt noch: {missing.join(", ")}</span>}
          </div>
        </div>
        <button onClick={() => setShowPreview(v => !v)} className={cn("xl:hidden flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted", showPreview && "bg-muted")}><Eye className="w-4 h-4" /> Vorschau</button>
        <a href={`/api/writing/${doc.id}/pdf`} target="_blank" rel="noreferrer" onClick={() => { void flush(); }}
           className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted"><FileDown className="w-4 h-4" /> PDF</a>
        <button onClick={async () => { await flush(); setDialog("file"); }} className="flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted"><FolderInput className="w-4 h-4" /> Ablegen</button>
        <button onClick={async () => { await flush(); setDialog("send"); }} className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium"><Send className="w-4 h-4" /> Senden</button>
        {extra}
      </header>

      <div className="flex-1 min-h-0 grid xl:grid-cols-[minmax(0,1fr)_minmax(0,0.8fr)]">
        <div className={cn("min-h-0 overflow-y-auto p-4 md:p-6", showPreview && "hidden xl:block")}>
          <div className="mx-auto max-w-[720px] grid gap-4">
            <section className="grid gap-2 sm:grid-cols-2">
              <div className="relative grid gap-1">
                <label className="text-[11px] text-muted-foreground" htmlFor="w-name">An</label>
                <div className="relative">
                  <input id="w-name" disabled={final} value={name} onChange={e => { setName(e.target.value); setContactId(null); touch(); }} placeholder="Name oder Firma" autoComplete="off" className={cn(input, "w-full pr-8")} />
                  {contactId ? <Check className="absolute right-2.5 top-2.5 w-4 h-4 text-emerald-500" aria-label="Aus den Kontakten" /> : <Search className="absolute right-2.5 top-2.5 w-4 h-4 text-muted-foreground" />}
                </div>
                {hits && hits.length > 0 && (
                  <ul className="absolute z-20 top-full mt-1 w-full rounded-lg border border-border bg-card shadow-xl overflow-hidden">
                    {hits.map(c => (
                      <li key={c.id}><button onMouseDown={e => e.preventDefault()} onClick={() => pick(c)} className="w-full text-left px-3 py-2 text-sm hover:bg-muted">
                        {c.display_name}{c.legal_name && c.legal_name !== c.display_name ? <span className="text-muted-foreground"> · {c.legal_name}</span> : null}
                      </button></li>
                    ))}
                    <li><button onMouseDown={e => e.preventDefault()} onClick={() => setHits(null)} className="w-full text-left px-3 py-1.5 text-[11px] text-muted-foreground hover:bg-muted flex items-center gap-1"><X className="w-3 h-3" /> keiner davon</button></li>
                  </ul>
                )}
                <label className="text-[11px] text-muted-foreground mt-1" htmlFor="w-mail">E-Mail (für „Senden“)</label>
                <input id="w-mail" disabled={final} value={email} onChange={e => { setEmail(e.target.value); touch(); }} placeholder="optional" className={input} />
              </div>
              <div className="grid gap-1 content-start">
                <label className="text-[11px] text-muted-foreground" htmlFor="w-addr">Adresse (eine Zeile pro Teil)</label>
                <textarea id="w-addr" disabled={final} value={address} onChange={e => { setAddress(e.target.value); touch(); }} rows={4} placeholder={"Straße und Hausnummer\nPLZ Ort"}
                          className={cn(input, "resize-none", !final && !address.trim() && "border-amber-500/50")} />
              </div>
            </section>

            <div className="rounded-xl bg-white shadow-lg ring-1 ring-black/10 px-6 py-5 md:px-10 md:py-8">
              <input disabled={final} value={subject} onChange={e => { setSubject(e.target.value); touch(); }} placeholder="Betreff" aria-label="Betreff"
                     className="w-full bg-transparent text-[17px] font-bold text-zinc-900 placeholder:text-zinc-400 outline-none border-b border-zinc-200 pb-2 mb-4" />
              {!final && editor && (
                <div className="flex items-center gap-0.5 mb-3 -ml-1.5 [&_button]:text-zinc-500 [&_button:hover]:bg-zinc-100">
                  <Tool on={() => editor.chain().focus().toggleBold().run()} active={editor.isActive("bold")} label="Fett"><Bold className="w-4 h-4" /></Tool>
                  <Tool on={() => editor.chain().focus().toggleItalic().run()} active={editor.isActive("italic")} label="Kursiv"><Italic className="w-4 h-4" /></Tool>
                  <Tool on={() => editor.chain().focus().toggleUnderline().run()} active={editor.isActive("underline")} label="Unterstrichen"><UnderlineIcon className="w-4 h-4" /></Tool>
                  <Tool on={() => editor.chain().focus().toggleBulletList().run()} active={editor.isActive("bulletList")} label="Liste"><List className="w-4 h-4" /></Tool>
                  <Tool on={() => editor.chain().focus().toggleOrderedList().run()} active={editor.isActive("orderedList")} label="Nummerierte Liste"><ListOrdered className="w-4 h-4" /></Tool>
                  <span className="w-px h-4 bg-zinc-200 mx-1" />
                  <Tool on={() => editor.chain().focus().undo().run()} label="Rückgängig"><Undo2 className="w-4 h-4" /></Tool>
                  <Tool on={() => editor.chain().focus().redo().run()} label="Wiederholen"><Redo2 className="w-4 h-4" /></Tool>
                </div>
              )}
              <EditorContent editor={editor} />
            </div>

            {!final && (
              <>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input type="checkbox" checked={closing} onChange={e => { setClosing(e.target.checked); touch(); }} />
                  Grußformel und Name aus dem Briefpapier anhängen (aus, wenn der Text schon selbst grüßt)
                </label>
                <form onSubmit={e => { e.preventDefault(); void rewrite(); }} className="flex items-center gap-2 rounded-xl border border-border bg-card p-2">
                  <Sparkles className="w-4 h-4 text-primary shrink-0 ml-1" />
                  <input value={wish} onChange={e => setWish(e.target.value)} disabled={rewriting} placeholder="Yorik, überarbeite: freundlicher, kürzer, bestimmter … (markierter Text oder der ganze Brief)"
                         className="flex-1 min-w-0 bg-transparent text-sm outline-none placeholder:text-muted-foreground" />
                  <button type="submit" disabled={rewriting || !wish.trim()} className="rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium disabled:opacity-50 flex items-center gap-1.5">
                    {rewriting && <Loader2 className="w-4 h-4 animate-spin" />} Überarbeiten
                  </button>
                </form>
              </>
            )}
          </div>
        </div>

        <div className={cn("min-h-0 overflow-y-auto border-l border-border bg-muted/30 p-4", showPreview ? "block" : "hidden xl:block")}>
          <div ref={boxRef} className="mx-auto max-w-[620px] rounded-lg overflow-hidden bg-[#d9d9de]" style={{ height: Math.round(1190 * scale) }}>
            {/* the page brings its own styles and no scripts; the frame allows none */}
            <iframe title="Vorschau" sandbox="" srcDoc={html} tabIndex={-1}
                    style={{ width: SHEET_PX, height: 1190, border: 0, transform: `scale(${scale})`, transformOrigin: "top left", pointerEvents: "none" }} />
          </div>
          <p className="mt-2 text-center text-[11px] text-muted-foreground">So wird die erste Seite gedruckt. Das Aussehen kommt aus deinem Briefpapier.</p>
        </div>
      </div>

      {dialog === "send" && <SendDialog doc={doc} to={email} subject={subject} onClose={() => setDialog(null)} onDone={d => { setDialog(null); onChanged(d); say("Verschickt. Der Brief ist jetzt fertig und liegt unter „Verschickt und abgelegt“."); }} />}
      {dialog === "file" && <FileDialog doc={doc} onClose={() => setDialog(null)} onDone={d => { setDialog(null); onChanged(d); say("In Paperless abgelegt. Der Brief ist jetzt fertig."); }} />}
    </div>
  );
}
