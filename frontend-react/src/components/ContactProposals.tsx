/**
 * ContactProposals — suggestions Yorik makes about contacts, decided by a
 * person. Two kinds today:
 *   merge        "the number in Anna's email signature belongs to the
 *                WhatsApp contact Anna" → pick which one survives
 *   add_channel  "+49… appears in this contact's signature" → add it
 * Merges are recorded and can be undone from the same panel.
 * Backend: backend/contact_identity.py, routes in contact_identity_routes.py.
 */
import { useCallback, useEffect, useState } from "react";
import { GitMerge, Loader2, Plus, Undo2, X } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "@/components/Toast";

type Channel = { kind: string; value: string };
type Contact = { id: number; display_name: string; kind: string; status: string; channels?: Channel[] };
type Proposal = {
  id: number;
  kind: "merge" | "add_channel";
  contact_id: number;
  other_contact_id?: number | null;
  channel_kind?: string | null;
  channel_value?: string | null;
  reason: string;
  confidence: number;
  created_at: string;
  contact: Contact | null;
  other_contact: Contact | null;
};
type Merge = { id: number; keep_id: number; drop_id: number; keep_name: string; drop_name: string; created_at: string; undone_at: string | null };

function channels(c: Contact | null): string {
  if (!c?.channels?.length) return "no contact details";
  return c.channels.map(ch => ch.kind === "whatsapp" ? `WhatsApp ${ch.value.split("@")[0]}` : ch.value).join(" · ");
}

export function ContactProposals({ onChanged }: { onChanged: () => void }) {
  const [items, setItems] = useState<Proposal[]>([]);
  const [merges, setMerges] = useState<Merge[]>([]);
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, m] = await Promise.all([
        api.get<{ proposals: Proposal[] }>("/api/contacts/proposals"),
        api.get<{ merges: Merge[] }>("/api/contacts/merges"),
      ]);
      setItems(p.proposals || []);
      setMerges((m.merges || []).filter(x => !x.undone_at).slice(0, 3));
    } catch {
      /* not visible for this role, or backend older than the panel */
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function accept(p: Proposal, keepId?: number) {
    setBusy(p.id);
    try {
      await api.post(`/api/contacts/proposals/${p.id}/accept`, keepId ? { keep_id: keepId } : {});
      toast(p.kind === "merge" ? "Contacts merged. You can undo this below." : "Added.");
      await load();
      onChanged();
    } catch (e: any) {
      toast(`Couldn't apply: ${e?.message || e}`);
    } finally {
      setBusy(null);
    }
  }
  async function reject(p: Proposal) {
    setBusy(p.id);
    try {
      await api.post(`/api/contacts/proposals/${p.id}/reject`, {});
      await load();
    } catch (e: any) {
      toast(`Couldn't dismiss: ${e?.message || e}`);
    } finally {
      setBusy(null);
    }
  }
  async function undo(m: Merge) {
    setBusy(-m.id);
    try {
      await api.post(`/api/contacts/merges/${m.id}/undo`, {});
      toast(`Restored ${m.drop_name}.`);
      await load();
      onChanged();
    } catch (e: any) {
      toast(`Couldn't undo: ${e?.message || e}`);
    } finally {
      setBusy(null);
    }
  }

  if (items.length === 0 && merges.length === 0) return null;

  return (
    <div className="mb-4 rounded-xl border border-amber-300/60 bg-amber-50/60 dark:bg-amber-950/20 dark:border-amber-700/50 p-3">
      {items.length > 0 && (
        <div className="text-xs uppercase tracking-wider text-amber-800 dark:text-amber-300 mb-2">
          Suggestions · {items.length}
        </div>
      )}
      <ul className="space-y-3">
        {items.map(p => (
          <li key={p.id} className="bg-card border border-border rounded-lg p-3">
            <div className="text-sm">{p.reason}</div>
            {p.kind === "merge" && p.contact && p.other_contact && (
              <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-2">
                {[p.contact, p.other_contact].map(c => (
                  <button
                    key={c.id}
                    disabled={busy === p.id}
                    onClick={() => accept(p, c.id)}
                    className="text-left rounded-md border border-border px-3 py-2 hover:border-violet-500 hover:bg-violet-500/5 transition disabled:opacity-50"
                    title="Keep this one, fold the other into it"
                  >
                    <div className="text-sm font-medium flex items-center gap-1.5">
                      <GitMerge className="w-3.5 h-3.5 text-violet-500" /> Keep {c.display_name}
                    </div>
                    <div className="text-[11px] text-muted-foreground mt-0.5">{channels(c)}</div>
                  </button>
                ))}
              </div>
            )}
            {p.kind === "add_channel" && p.contact && (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button
                  disabled={busy === p.id}
                  onClick={() => accept(p)}
                  className="text-xs px-3 py-1.5 rounded-md bg-primary text-primary-foreground inline-flex items-center gap-1.5 disabled:opacity-50"
                >
                  {busy === p.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
                  Add {p.channel_value} to {p.contact.display_name}
                </button>
              </div>
            )}
            <div className="mt-2">
              <button
                disabled={busy === p.id}
                onClick={() => reject(p)}
                className="text-[11px] text-muted-foreground hover:text-foreground inline-flex items-center gap-1 disabled:opacity-50"
              >
                <X className="w-3 h-3" /> Not the same person
              </button>
            </div>
          </li>
        ))}
      </ul>
      {merges.length > 0 && (
        <div className="mt-3 pt-2 border-t border-amber-300/40 text-[11px] text-muted-foreground flex flex-wrap gap-x-4 gap-y-1">
          {merges.map(m => (
            <span key={m.id} className="inline-flex items-center gap-1">
              {m.drop_name} → {m.keep_name}
              <button
                disabled={busy === -m.id}
                onClick={() => undo(m)}
                className="inline-flex items-center gap-0.5 text-violet-600 hover:underline disabled:opacity-50"
              >
                <Undo2 className="w-3 h-3" /> undo
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
