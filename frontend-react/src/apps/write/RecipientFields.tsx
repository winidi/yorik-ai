/**
 * RecipientFields — who the document goes to: a name (with a search in
 * the contacts that brings the address along), the address as lines, an
 * e-mail address for "Senden". Typed values belong to the parent; a
 * picked contact is resolved by the server and handed back whole.
 */
import { useEffect, useState } from "react";
import { Check, Search, X } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { Recipient, WrittenDoc } from "./types";

type ContactHit = { id: number; display_name: string; legal_name?: string | null };
export interface RecipientValue { name: string; address: string; email: string; contactId: number | null }

export const recipientOf = (doc: WrittenDoc): RecipientValue => ({
  name: doc.recipient?.name || "", address: (doc.recipient?.address_lines || []).join("\n"),
  email: doc.recipient?.email || "", contactId: doc.recipient?.contact_id ?? null,
});
export const recipientPayload = (v: RecipientValue) => ({ contact_id: v.contactId, name: v.name, address_lines: v.address.split("\n"), email: v.email });

export function RecipientFields({ docId, value, onChange, onPicked, disabled, label = "An", say }: {
  docId: number; value: RecipientValue; onChange: (v: RecipientValue) => void;
  /** a contact was chosen: the server resolved it and saved the document */
  onPicked: (doc: WrittenDoc) => void;
  disabled?: boolean; label?: string; say: (text: string, bad?: boolean) => void;
}) {
  const [hits, setHits] = useState<ContactHit[] | null>(null);
  const { name, contactId } = value;
  useEffect(() => {
    if (disabled || contactId || name.trim().length < 2) { setHits(null); return; }
    const t = window.setTimeout(async () => {
      try { setHits((await api.get<ContactHit[]>(`/api/contacts?q=${encodeURIComponent(name.trim())}&limit=6`)).slice(0, 6)); } catch { setHits(null); }
    }, 300);
    return () => window.clearTimeout(t);
  }, [name, contactId, disabled]);

  async function pick(c: ContactHit) {
    setHits(null);
    try {
      const out = await api.patch<WrittenDoc>(`/api/writing/${docId}`, { contact_id: c.id });
      const r = out.recipient as Recipient;
      onPicked(out);
      if (!r.address_lines?.length) say("Für diesen Kontakt ist keine Adresse hinterlegt. Du kannst sie hier eintragen.");
    } catch (e: any) { say(`Kontakt übernehmen hat nicht geklappt: ${e?.message || e}`, true); }
  }

  const input = "rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary disabled:opacity-70";
  return (
    <section className="grid gap-2 sm:grid-cols-2">
      <div className="relative grid gap-1 content-start">
        <label className="text-[11px] text-muted-foreground" htmlFor="w-name">{label}</label>
        <div className="relative">
          <input id="w-name" disabled={disabled} value={value.name} onChange={e => onChange({ ...value, name: e.target.value, contactId: null })}
                 placeholder="Name oder Firma" autoComplete="off" className={cn(input, "w-full pr-8")} />
          {contactId ? <Check className="absolute right-2.5 top-2.5 w-4 h-4 text-emerald-500" aria-label="Aus den Kontakten" /> : <Search className="absolute right-2.5 top-2.5 w-4 h-4 text-muted-foreground" />}
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
        </div>
        <label className="text-[11px] text-muted-foreground mt-1" htmlFor="w-mail">E-Mail (für „Senden“)</label>
        <input id="w-mail" disabled={disabled} value={value.email} onChange={e => onChange({ ...value, email: e.target.value })} placeholder="optional" className={input} />
      </div>
      <div className="grid gap-1 content-start">
        <label className="text-[11px] text-muted-foreground" htmlFor="w-addr">Adresse (eine Zeile pro Teil)</label>
        <textarea id="w-addr" disabled={disabled} value={value.address} onChange={e => onChange({ ...value, address: e.target.value })} rows={4} placeholder={"Straße und Hausnummer\nPLZ Ort"}
                  className={cn(input, "resize-none", !disabled && !value.address.trim() && "border-amber-500/50")} />
      </div>
    </section>
  );
}
