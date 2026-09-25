/**
 * LetterheadCard — Settings → You. Your letterhead: how every letter,
 * invoice and quote you write looks. Logo, accent colour, font, sender,
 * footer (bank, tax numbers) and the standard sentences; on the right the
 * live preview, rendered by the same layout code that makes the PDF.
 * Backend: /api/letterheads (backend/writing/routes.py).
 *
 * The data model allows several letterheads per person; this card shows
 * the default one.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { FileDown, ImagePlus, Loader2, Save, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

type Data = Record<string, string | number | boolean>;
type Letterhead = { id: number; name: string; is_default: boolean; data: Data; logo_url: string | null };
type Font = { id: string; label: string; css: string };
type Kind = "letter" | "invoice" | "quote";

const KINDS: Array<{ id: Kind; label: string }> = [{ id: "letter", label: "Letter" }, { id: "invoice", label: "Invoice" }, { id: "quote", label: "Quote" }];
const ACCENTS = ["#1f3a5f", "#0a7d4f", "#b43c5a", "#6b4fd3", "#c2571a", "#27272a"];
const SHEET_PX = 794 + 60;     // 210 mm at 96 dpi plus the preview's grey edge

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <fieldset className="grid grid-cols-2 gap-x-3 gap-y-2.5">
      <legend className="col-span-2 mb-1.5 text-xs font-semibold text-muted-foreground">{title}</legend>
      {children}
    </fieldset>
  );
}

export function LetterheadCard({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [lh, setLh] = useState<Letterhead | null>(null);
  const [fonts, setFonts] = useState<Font[]>([]);
  const [form, setForm] = useState<Data>({});
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [kind, setKind] = useState<Kind>("letter");
  const [html, setHtml] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(0.45);

  const load = useCallback(async () => {
    try {
      const r = await api.get<{ letterheads: Letterhead[]; fonts: Font[] }>("/api/letterheads");
      const mine = r.letterheads.find(l => l.is_default) || r.letterheads[0];
      setLh(mine); setFonts(r.fonts); setForm(mine.data); setDirty(false);
    } catch (e: any) {
      // a frontend that is ahead of the running backend: no card, no alarm
      if (e?.status !== 404) toast(`Couldn't load your letterhead: ${e?.message || e}`, "error");
    }
  }, [toast]);
  useEffect(() => { load(); }, [load]);

  // live preview: what the form says now, a moment after the last keystroke
  const lhId = lh?.id, logoUrl = lh?.logo_url;
  useEffect(() => {
    if (!lhId) return;
    const t = setTimeout(async () => {
      try { setHtml((await api.post<{ html: string }>("/api/letterheads/preview", { kind, letterhead_id: lhId, data: form })).html); } catch {}
    }, 350);
    return () => clearTimeout(t);
  }, [form, kind, lhId, logoUrl]);

  useLayoutEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    // measured at once as well: an observer reports only when the page
    // is being painted, and the sheet must fit from the first frame
    const fit = () => { if (el.clientWidth) setScale(Math.min(1, el.clientWidth / SHEET_PX)); };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [lhId]);

  const set = (key: string, value: string | number | boolean) => { setForm(f => ({ ...f, [key]: value })); setDirty(true); };

  async function save() {
    if (!lh) return;
    setBusy("save");
    try {
      const out = await api.patch<Letterhead>(`/api/letterheads/${lh.id}`, { data: form });
      setLh(out); setForm(out.data); setDirty(false);
      toast("Letterhead saved", "success");
    } catch (e: any) { toast(`Couldn't save: ${e?.message || e}`, "error"); }
    finally { setBusy(null); }
  }

  async function uploadLogo(file: File) {
    if (!lh) return;
    setBusy("logo");
    try {
      const fd = new FormData(); fd.append("image", file, file.name);
      const out = await api.postForm<Letterhead>(`/api/letterheads/${lh.id}/logo`, fd);
      setLh(l => l && { ...l, logo_url: out.logo_url });
      toast("Logo saved", "success");
    } catch (e: any) { toast(`Couldn't upload: ${e?.message || e}`, "error"); }
    finally { setBusy(null); if (fileRef.current) fileRef.current.value = ""; }
  }

  async function removeLogo() {
    if (!lh) return;
    setBusy("logo");
    try { const out = await api.delete<Letterhead>(`/api/letterheads/${lh.id}/logo`); setLh(l => l && { ...l, logo_url: out.logo_url }); }
    catch (e: any) { toast(`Couldn't remove: ${e?.message || e}`, "error"); }
    finally { setBusy(null); }
  }

  if (!lh) return null;
  const text = (key: string) => String(form[key] ?? "");
  // a plain function, not a component: a component declared in here would
  // be a new one on every render and the field would lose the cursor
  const field = (k: string, label: string, wide = false) => (
    <label key={k} className={cn("grid gap-1 text-xs text-muted-foreground", wide && "col-span-2")}>
      {label}
      <input value={text(k)} onChange={e => set(k, e.target.value)}
             className="rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary" />
    </label>
  );

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <h3 className="text-xs font-semibold text-muted-foreground mb-1">Your letterhead</h3>
      <p className="text-xs text-muted-foreground mb-4">
        How your letters, invoices and quotes look — always the same, whoever or whatever writes the text. Started from your profile; change what you like.
      </p>
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="grid gap-5 content-start">
          <Section title="Look">
            <div className="col-span-2 flex flex-wrap items-center gap-2">
              {ACCENTS.map(c => (
                <button key={c} onClick={() => set("accent", c)} title={c} aria-label={`Accent ${c}`}
                        className={cn("w-7 h-7 rounded-full ring-offset-2 ring-offset-background transition", text("accent").toLowerCase() === c ? "ring-2 ring-foreground scale-110" : "hover:scale-105")}
                        style={{ background: c }} />
              ))}
              <input type="color" value={text("accent") || "#1f3a5f"} onChange={e => set("accent", e.target.value)}
                     className="w-8 h-8 rounded-full border-0 bg-transparent cursor-pointer" aria-label="Custom accent colour" />
            </div>
            <label className="grid gap-1 text-xs text-muted-foreground">
              Font
              <select value={text("font")} onChange={e => set("font", e.target.value)}
                      className="rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary">
                {fonts.map(f => <option key={f.id} value={f.id}>{f.label}</option>)}
              </select>
            </label>
            <label className="grid gap-1 text-xs text-muted-foreground">
              Logo position
              <select value={text("logo_place")} onChange={e => set("logo_place", e.target.value)}
                      className="rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary">
                <option value="right">Right</option><option value="left">Left</option><option value="center">Centred</option>
              </select>
            </label>
            <label className="col-span-2 grid gap-1 text-xs text-muted-foreground">
              Letter style
              <select value={text("style") || "auto"} onChange={e => set("style", e.target.value)}
                      className="rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary">
                <option value="auto">Automatic — private letter unless a business name is set</option>
                <option value="private">Private letter — your name once above the address and under the letter</option>
                <option value="business">Business letterhead — name at the top and in the footer</option>
              </select>
            </label>
            <div className="col-span-2 flex items-center gap-2">
              <input ref={fileRef} type="file" accept="image/*" hidden onChange={e => { const f = e.target.files?.[0]; if (f) void uploadLogo(f); }} />
              {lh.logo_url && <img src={lh.logo_url} alt="" className="h-9 max-w-[120px] object-contain rounded bg-white p-1" />}
              <button onClick={() => fileRef.current?.click()} disabled={busy === "logo"}
                      className="flex items-center gap-2 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50">
                {busy === "logo" ? <Loader2 className="w-4 h-4 animate-spin" /> : <ImagePlus className="w-4 h-4" />} {lh.logo_url ? "Change logo" : "Add logo"}
              </button>
              {lh.logo_url && (
                <button onClick={removeLogo} disabled={busy === "logo"} className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted disabled:opacity-50">
                  <Trash2 className="w-4 h-4" /> Remove
                </button>
              )}
            </div>
          </Section>

          <Section title="Sender">
            {field("sender_name", "Your name")}
            {field("business_name", "Business (optional)")}
            {field("street", "Street and number", true)}
            {field("postcode", "Postcode")}
            {field("city", "City")}
            {field("phone", "Phone")}
            {field("email", "Email")}
            {field("website", "Website")}
            {field("country", "Country (DE, AT, …)")}
          </Section>

          <Section title="Footer: bank and tax">
            {field("bank_name", "Bank")}
            {field("iban", "IBAN")}
            {field("bic", "BIC")}
            {field("vat_id", "VAT ID (USt-IdNr.)")}
            {field("tax_id", "Tax number (Steuernummer)")}
            {field("register", "Register (e.g. Amtsgericht, HRB)")}
          </Section>

          <Section title="Standard sentences">
            {field("closing", "Closing")}
            {field("signature_name", "Name under the closing")}
            <label className="grid gap-1 text-xs text-muted-foreground">
              Payment within (days)
              <input type="number" min={0} max={365} value={Number(form.payment_days ?? 14)} onChange={e => set("payment_days", Number(e.target.value))}
                     className="rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary" />
            </label>
            <label className="flex items-center gap-2 text-xs text-foreground self-end pb-1.5">
              <input type="checkbox" checked={!!form.small_business} onChange={e => set("small_business", e.target.checked)} />
              Small business (§ 19 UStG, no VAT)
            </label>
            <label className="col-span-2 grid gap-1 text-xs text-muted-foreground">
              Payment sentence — {"{faellig}"} becomes the due date, {"{betrag}"} the amount
              <textarea value={text("payment_text")} onChange={e => set("payment_text", e.target.value)} rows={2}
                        className="rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary resize-y" />
            </label>
          </Section>

          <div className="flex items-center gap-2 flex-wrap">
            <button onClick={save} disabled={!dirty || busy === "save"}
                    className="flex items-center gap-2 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm font-medium disabled:opacity-50">
              {busy === "save" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} Save letterhead
            </button>
            <a href={`/api/letterheads/${lh.id}/sample.pdf?kind=${kind}`} target="_blank" rel="noreferrer"
               className={cn("flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-muted", dirty && "pointer-events-none opacity-50")}
               title={dirty ? "Save first: the PDF shows the saved letterhead" : "Open a sample PDF"}>
              <FileDown className="w-4 h-4" /> Sample PDF
            </a>
            {dirty && <span className="text-xs text-muted-foreground">Not saved yet — the preview already shows it.</span>}
          </div>
        </div>

        <div className="min-w-0">
          <div className="mb-2 flex gap-1">
            {KINDS.map(k => (
              <button key={k.id} onClick={() => setKind(k.id)}
                      className={cn("rounded-full px-3 py-1 text-xs font-medium", kind === k.id ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}>{k.label}</button>
            ))}
          </div>
          <div ref={boxRef} className="rounded-lg overflow-hidden border border-border bg-[#d9d9de] lg:sticky lg:top-4" style={{ height: Math.round(1190 * scale) }}>
            {/* the sample page brings its own styles and no scripts; the frame allows none */}
            <iframe title="Letterhead preview" sandbox="" srcDoc={html} tabIndex={-1}
                    style={{ width: SHEET_PX, height: 1190, border: 0, transform: `scale(${scale})`, transformOrigin: "top left", pointerEvents: "none" }} />
          </div>
        </div>
      </div>
    </div>
  );
}
