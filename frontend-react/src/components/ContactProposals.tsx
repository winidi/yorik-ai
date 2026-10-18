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
import { useTranslation } from "react-i18next";
import i18n from "@/i18n";

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
  evidence?: { source?: string; phone?: string; lid?: string; number?: string } | null;
  confidence: number;
  created_at: string;
  contact: Contact | null;
  other_contact: Contact | null;
};
type Merge = { id: number; keep_id: number; drop_id: number; keep_name: string; drop_name: string; created_at: string; undone_at: string | null };

function channels(c: Contact | null): string {
  if (!c?.channels?.length) return i18n.t("contacts.suggestions.noDetails");
  // A WhatsApp LID is the account's internal id, not a number anyone
  // dials: it reads as "WhatsApp account", a real number as +49….
  return c.channels.map(ch => ch.kind === "whatsapp"
    ? (ch.value.endsWith("@lid") ? i18n.t("contacts.suggestions.waAccount") : `WhatsApp +${ch.value.split("@")[0]}`)
    : ch.value).join(" · ");
}

/** The reason in the person's language, built from the evidence; the
 *  stored English sentence only when the source is unknown. */
function reasonText(p: Proposal, t: (k: string, o?: Record<string, unknown>) => string): string {
  const src = p.evidence?.source;
  const a = p.other_contact?.display_name || "?";
  const b = p.contact?.display_name || "?";
  if (p.kind === "merge" && src === "whatsapp_alias_map")
    return t("contacts.suggestions.reason.whatsappTwin", { a, b, number: "+" + (p.evidence?.number || "").split("@")[0].replace(/\D/g, "") });
  if (p.kind === "merge" && src === "email_signature")
    return t("contacts.suggestions.reason.signatureMerge", { phone: p.evidence?.phone || "", name: a });
  if (p.kind === "add_channel" && src === "email_signature")
    return t("contacts.suggestions.reason.signatureAdd", { phone: p.channel_value || "" });
  if (p.kind === "merge" && src === "vcard_import")
    return t("contacts.suggestions.reason.vcardImport", { a, b });
  return p.reason;
}

function sameName(a: Contact, b: Contact): boolean {
  return a.display_name.trim().toLowerCase() === b.display_name.trim().toLowerCase();
}

export function ContactProposals({ onChanged, reloadKey = 0 }: { onChanged: () => void; reloadKey?: number }) {
  const { t } = useTranslation();
  const [items, setItems] = useState<Proposal[]>([]);
  const [merges, setMerges] = useState<Merge[]>([]);
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, m] = await Promise.all([
        api.get<{ proposals: Proposal[] }>("/api/contacts/proposals"),
        api.get<{ merges: Merge[] }>("/api/contacts/merges"),
      ]);
      // merges first — they are the rare, valuable ones; number
      // suggestions come in bulk after a signature scan
      setItems([...(p.proposals || [])].sort((a, b) => (a.kind === b.kind ? 0 : a.kind === "merge" ? -1 : 1)));
      setMerges((m.merges || []).filter(x => !x.undone_at).slice(0, 3));
    } catch {
      /* not visible for this role, or backend older than the panel */
    }
  }, []);
  useEffect(() => { load(); }, [load, reloadKey]);

  async function accept(p: Proposal, keepId?: number) {
    setBusy(p.id);
    try {
      await api.post(`/api/contacts/proposals/${p.id}/accept`, keepId ? { keep_id: keepId } : {});
      toast(p.kind === "merge" ? t("contacts.suggestions.merged") : t("contacts.suggestions.added"));
      await load();
      onChanged();
    } catch (e: any) {
      toast(t("contacts.suggestions.applyFailed", { error: e?.message || e }));
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
      toast(t("contacts.suggestions.dismissFailed", { error: e?.message || e }));
    } finally {
      setBusy(null);
    }
  }
  async function undo(m: Merge) {
    setBusy(-m.id);
    try {
      await api.post(`/api/contacts/merges/${m.id}/undo`, {});
      toast(t("contacts.suggestions.restored", { name: m.drop_name }));
      await load();
      onChanged();
    } catch (e: any) {
      toast(t("contacts.suggestions.undoFailed", { error: e?.message || e }));
    } finally {
      setBusy(null);
    }
  }

  async function rejectAllAdds() {
    setBusy(-999999);
    try {
      await api.post(`/api/contacts/proposals/reject-all?kind=add_channel`, {});
      await load();
    } catch (e: any) {
      toast(t("contacts.suggestions.dismissFailed", { error: e?.message || e }));
    } finally {
      setBusy(null);
    }
  }

  if (items.length === 0 && merges.length === 0) return null;
  const merges_n = items.filter(i => i.kind === "merge").length;
  const adds_n = items.length - merges_n;

  return (
    <div className="mb-4 rounded-xl border border-amber-300/60 bg-amber-50/60 dark:bg-amber-950/20 dark:border-amber-700/50 p-3">
      {items.length > 0 && (
        <div className="text-xs text-amber-800 dark:text-amber-300 mb-2 flex items-center gap-3">
          <span>{t("contacts.suggestions.heading")}{merges_n > 0 && t("contacts.suggestions.merges", { count: merges_n })}{merges_n > 0 && adds_n > 0 && " · "}{adds_n > 0 && t("contacts.suggestions.numbers", { count: adds_n })}</span>
          {adds_n > 1 && (
            <button
              disabled={busy === -999999}
              onClick={rejectAllAdds}
              className="normal-case tracking-normal text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
            >
              {t("contacts.suggestions.dismissAllNumbers")}
            </button>
          )}
        </div>
      )}
      <ul className="space-y-3">
        {items.map(p => (
          <li key={p.id} className="bg-card border border-border rounded-lg p-3">
            <div className="text-sm">{reasonText(p, t)}</div>
            {p.kind === "merge" && p.contact && p.other_contact && (
              // Both rows become one contact with every number and account
              // of both; the only choice is the name. Same name on both
              // sides: nothing to choose, one button.
              <div className="mt-2">
                {sameName(p.contact, p.other_contact) ? (
                  <button
                    disabled={busy === p.id}
                    onClick={() => accept(p, p.contact!.id)}
                    className="text-xs px-3 py-1.5 rounded-md bg-primary text-primary-foreground inline-flex items-center gap-1.5 disabled:opacity-50"
                  >
                    {busy === p.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <GitMerge className="w-3.5 h-3.5" />}
                    {t("contacts.suggestions.merge")}
                  </button>
                ) : (
                  <>
                    <div className="text-xs text-muted-foreground mb-1.5">{t("contacts.suggestions.mergeAs")}</div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                      {[p.contact, p.other_contact].map(c => (
                        <button
                          key={c.id}
                          disabled={busy === p.id}
                          onClick={() => accept(p, c.id)}
                          className="text-left rounded-md border border-border px-3 py-2 hover:border-violet-500 hover:bg-violet-500/5 transition disabled:opacity-50"
                          title={t("contacts.suggestions.keepTitle")}
                        >
                          <div className="text-sm font-medium flex items-center gap-1.5">
                            <GitMerge className="w-3.5 h-3.5 text-violet-500" /> {c.display_name}
                          </div>
                          <div className="text-xs text-muted-foreground mt-0.5">{channels(c)}</div>
                        </button>
                      ))}
                    </div>
                  </>
                )}
                <div className="text-xs text-muted-foreground mt-1.5">{t("contacts.suggestions.mergeHint")}</div>
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
                  {t("contacts.suggestions.addTo", { value: p.channel_value, name: p.contact.display_name })}
                </button>
              </div>
            )}
            <div className="mt-2">
              <button
                disabled={busy === p.id}
                onClick={() => reject(p)}
                className="text-xs text-muted-foreground hover:text-foreground inline-flex items-center gap-1 disabled:opacity-50"
              >
                <X className="w-3 h-3" /> {t("contacts.suggestions.notSamePerson")}
              </button>
            </div>
          </li>
        ))}
      </ul>
      {merges.length > 0 && (
        <div className="mt-3 pt-2 border-t border-amber-300/40 text-xs text-muted-foreground flex flex-wrap gap-x-4 gap-y-1">
          {merges.map(m => (
            <span key={m.id} className="inline-flex items-center gap-1">
              {m.drop_name} → {m.keep_name}
              <button
                disabled={busy === -m.id}
                onClick={() => undo(m)}
                className="inline-flex items-center gap-0.5 text-violet-600 hover:underline disabled:opacity-50"
              >
                <Undo2 className="w-3 h-3" /> {t("contacts.suggestions.undo")}
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
