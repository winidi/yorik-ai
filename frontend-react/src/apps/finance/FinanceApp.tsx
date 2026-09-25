/**
 * FinanceApp — connect a bank account (read-only, FinTS) and see
 * synced transactions. The PIN field here is the ONLY place it's ever
 * typed; it goes straight to POST /api/bank/accounts in the user's own
 * browser, never through chat (see docs/plans/2026-09-25-finanzen.md).
 */
import { useEffect, useRef, useState } from "react";
import { Landmark, Plus, RefreshCw, Trash2, X, Loader2, Users, Lock, Search, Check } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Institute {
  blz: string;
  name: string;
  city: string;
  url: string;
}
import { Dock } from "@/components/Dock";

interface BankAccount {
  id: number;
  display_name: string;
  iban: string | null;
  space_id: number | null;
  last_synced_at: string | null;
  last_sync_error: string | null;
}

interface Transaction {
  id: number;
  booking_date: string;
  amount: number;
  currency: string;
  counterparty: string | null;
  purpose: string | null;
  category: string | null;
  account_id: number;
}

function eur(n: number): string {
  return n.toLocaleString("de-DE", { style: "currency", currency: "EUR" });
}

export function FinanceApp() {
  const [accounts, setAccounts] = useState<BankAccount[]>([]);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [days, setDays] = useState(30);
  const [syncingId, setSyncingId] = useState<number | null>(null);

  async function refresh() {
    setLoading(true);
    try {
      const [accs, txs] = await Promise.all([
        api.get<BankAccount[]>("/api/bank/accounts"),
        api.get<Transaction[]>(`/api/bank/transactions?days=${days}`),
      ]);
      setAccounts(accs);
      setTransactions(txs);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [days]);

  async function handleSync(id: number) {
    setSyncingId(id);
    try {
      await api.post(`/api/bank/accounts/${id}/sync`, {});
      await refresh();
    } finally {
      setSyncingId(null);
    }
  }

  async function handleDelete(id: number) {
    await api.delete(`/api/bank/accounts/${id}`);
    await refresh();
  }

  const byCategory = new Map<string, number>();
  for (const t of transactions) {
    const key = t.category || "unkategorisiert";
    byCategory.set(key, (byCategory.get(key) || 0) + t.amount);
  }
  const categoryRows = [...byCategory.entries()].sort((a, b) => a[1] - b[1]);

  return (
    <div className="h-screen overflow-y-auto bg-background">
    <div className="p-6 pb-24 max-w-3xl mx-auto w-full">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <span className="grid place-items-center w-9 h-9 rounded-lg bg-primary/15 text-primary">
            <Landmark className="w-5 h-5" />
          </span>
          <div>
            <h1 className="text-lg font-semibold">Finance</h1>
            <p className="text-xs text-muted-foreground">Nur lesend — Yorik kann nichts überweisen.</p>
          </div>
        </div>
        <button
          onClick={() => setShowForm(true)}
          className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium"
        >
          <Plus className="w-4 h-4" /> Konto verbinden
        </button>
      </div>

      {showForm && (
        <ConnectAccountForm
          onClose={() => setShowForm(false)}
          onConnected={() => { setShowForm(false); refresh(); }}
        />
      )}

      <section className="mb-8">
        <h2 className="text-sm font-medium text-muted-foreground mb-2">Konten</h2>
        {accounts.length === 0 && !loading && (
          <p className="text-sm text-muted-foreground">Noch kein Konto verbunden.</p>
        )}
        <div className="space-y-2">
          {accounts.map(a => (
            <div key={a.id} className="rounded-xl border border-border bg-card p-3 flex items-center gap-3">
              <span className="grid place-items-center w-8 h-8 rounded-lg bg-muted shrink-0">
                {a.space_id ? <Users className="w-4 h-4" /> : <Lock className="w-4 h-4" />}
              </span>
              <div className="min-w-0 flex-1">
                <div className="font-medium truncate">{a.display_name}</div>
                <div className="text-xs text-muted-foreground truncate">
                  {a.iban || "IBAN noch nicht bekannt"}
                  {a.space_id ? " · geteilt" : " · privat"}
                </div>
                {a.last_sync_error && (
                  <div className="text-xs text-rose-500 mt-0.5 truncate">Sync-Fehler: {a.last_sync_error}</div>
                )}
                {!a.last_sync_error && a.last_synced_at && (
                  <div className="text-[11px] text-muted-foreground mt-0.5">Zuletzt synchronisiert: {a.last_synced_at}</div>
                )}
              </div>
              <button
                onClick={() => handleSync(a.id)}
                disabled={syncingId === a.id}
                className="p-1.5 rounded-md hover:bg-muted text-muted-foreground disabled:opacity-50"
                title="Jetzt synchronisieren"
              >
                {syncingId === a.id ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
              </button>
              <button
                onClick={() => handleDelete(a.id)}
                className="p-1.5 rounded-md hover:bg-rose-500/10 text-muted-foreground hover:text-rose-500"
                title="Konto entfernen"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
          ))}
        </div>
      </section>

      {accounts.length > 0 && (
        <>
          <section className="mb-8">
            <div className="flex items-center justify-between mb-2">
              <h2 className="text-sm font-medium text-muted-foreground">Nach Kategorie</h2>
              <select
                value={days}
                onChange={e => setDays(Number(e.target.value))}
                className="text-xs rounded-md border border-border bg-background px-2 py-1"
              >
                <option value={30}>30 Tage</option>
                <option value={90}>90 Tage</option>
                <option value={180}>180 Tage</option>
              </select>
            </div>
            {categoryRows.length === 0 && <p className="text-sm text-muted-foreground">Noch keine Umsätze.</p>}
            <div className="space-y-1">
              {categoryRows.map(([cat, total]) => (
                <div key={cat} className="flex items-center justify-between text-sm py-1 border-b border-border/50">
                  <span className={cn(cat === "unkategorisiert" && "text-muted-foreground italic")}>{cat}</span>
                  <span className={cn("font-medium", total < 0 ? "text-rose-500" : "text-emerald-600")}>{eur(total)}</span>
                </div>
              ))}
            </div>
          </section>

          <section>
            <h2 className="text-sm font-medium text-muted-foreground mb-2">Umsätze</h2>
            <div className="space-y-1">
              {transactions.map(t => (
                <div key={t.id} className="flex items-center justify-between text-sm py-1.5 border-b border-border/50">
                  <div className="min-w-0 flex-1">
                    <div className="truncate">{t.counterparty || t.purpose || "—"}</div>
                    <div className="text-[11px] text-muted-foreground truncate">
                      {t.booking_date}{t.category ? ` · ${t.category}` : ""}
                    </div>
                  </div>
                  <span className={cn("font-medium shrink-0 ml-3", t.amount < 0 ? "text-rose-500" : "text-emerald-600")}>
                    {eur(t.amount)}
                  </span>
                </div>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
    <Dock activeAppId="finance" />
    </div>
  );
}

function ConnectAccountForm({ onClose, onConnected }: { onClose: () => void; onConnected: () => void }) {
  const [displayName, setDisplayName] = useState("");
  const [bankUrl, setBankUrl] = useState("");
  const [blz, setBlz] = useState("");
  const [loginName, setLoginName] = useState("");
  const [pin, setPin] = useState("");
  const [productId, setProductId] = useState("");
  const [shared, setShared] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await api.post("/api/bank/accounts", {
        display_name: displayName,
        bank_url: bankUrl,
        blz,
        login_name: loginName,
        pin,
        product_id: productId || undefined,
        shared,
      });
      onConnected();
    } catch (err: any) {
      setError(err?.message || "Verbindung fehlgeschlagen.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" onClick={onClose}>
      <form
        onSubmit={handleSubmit}
        onClick={e => e.stopPropagation()}
        className="bg-background rounded-xl border border-border shadow-xl w-full max-w-md p-5 space-y-3"
      >
        <div className="flex items-center justify-between mb-1">
          <div className="font-medium">Konto verbinden</div>
          <button type="button" onClick={onClose} className="p-1 rounded hover:bg-muted text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>

        <Field label="Name (z. B. Sparkasse, Gemeinsames Konto)">
          <input required value={displayName} onChange={e => setDisplayName(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm" />
        </Field>
        <Field label="Bank suchen" hint="Name oder Ort eingeben — BLZ und FinTS-Adresse werden automatisch ausgefüllt.">
          <BankSearchField
            onPick={inst => { setBankUrl(inst.url); setBlz(inst.blz); }}
          />
        </Field>
        <Field label="FinTS-Server-URL" hint="Automatisch ausgefüllt, wenn oben gefunden — sonst selbst eintragen (nachschlagen auf hbci-zka.de).">
          <input required value={bankUrl} onChange={e => setBankUrl(e.target.value)}
                placeholder="https://..."
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm" />
        </Field>
        <Field label="Bankleitzahl (BLZ)">
          <input required value={blz} onChange={e => setBlz(e.target.value)} maxLength={8}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm" />
        </Field>
        <Field label="Online-Banking-Zugangsnummer">
          <input required value={loginName} onChange={e => setLoginName(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm" />
        </Field>
        <Field label="PIN">
          <input required type="password" value={pin} onChange={e => setPin(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm" />
        </Field>
        <Field label="Produkt-ID (optional)"
              hint="Falls deine FinTS-Registrierung bei der DK noch nicht da ist, leer lassen — Yorik nutzt vorübergehend einen Platzhalter, ohne Garantie, dass jede Bank ihn akzeptiert.">
          <input value={productId} onChange={e => setProductId(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm" />
        </Field>
        <label className="flex items-center gap-2 text-sm pt-1">
          <input type="checkbox" checked={shared} onChange={e => setShared(e.target.checked)} />
          Gemeinsames Konto — für den ganzen Haushalt sichtbar
        </label>

        {error && <div className="text-sm text-rose-500">{error}</div>}

        <div className="flex justify-end pt-2">
          <button type="submit" disabled={saving}
                  className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium disabled:opacity-50">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
            Verbinden
          </button>
        </div>
      </form>
    </div>
  );
}

// Debounced search over the bundled FinTS institute list (GET
// /api/bank/institutes) — picking a result fills BLZ + FinTS URL so
// the user doesn't have to hunt one down by hand. Mirrors the
// contact-autocomplete pattern in Composer.tsx's RecipientField:
// short debounce, a request-id guard against out-of-order responses.
function BankSearchField({ onPick }: { onPick: (inst: Institute) => void }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Institute[]>([]);
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<Institute | null>(null);
  const reqIdRef = useRef(0);

  useEffect(() => {
    if (query.trim().length < 2) { setResults([]); setOpen(false); return; }
    const id = ++reqIdRef.current;
    const handle = window.setTimeout(async () => {
      try {
        const r = await api.get<Institute[]>(`/api/bank/institutes?q=${encodeURIComponent(query)}`);
        if (id !== reqIdRef.current) return;
        setResults(r);
        setOpen(r.length > 0);
      } catch {
        if (id === reqIdRef.current) { setResults([]); setOpen(false); }
      }
    }, 150);
    return () => window.clearTimeout(handle);
  }, [query]);

  function pick(inst: Institute) {
    setPicked(inst);
    setQuery(`${inst.name} (${inst.city})`);
    setOpen(false);
    onPick(inst);
  }

  return (
    <div className="relative">
      <div className="relative">
        <Search className="w-3.5 h-3.5 absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <input
          value={query}
          onChange={e => { setQuery(e.target.value); setPicked(null); }}
          onFocus={() => results.length > 0 && setOpen(true)}
          placeholder="z. B. Sparkasse Peine, ING"
          className="w-full rounded-md border border-border bg-background pl-7 pr-7 py-1.5 text-sm"
        />
        {picked && <Check className="w-3.5 h-3.5 absolute right-2 top-1/2 -translate-y-1/2 text-emerald-600" />}
      </div>
      {open && (
        <div className="absolute z-10 mt-1 w-full max-h-56 overflow-y-auto rounded-md border border-border bg-card shadow-lg">
          {results.map(inst => (
            <button
              type="button"
              key={inst.blz}
              onClick={() => pick(inst)}
              className="w-full text-left px-2.5 py-1.5 text-sm hover:bg-muted"
            >
              <div className="font-medium truncate">{inst.name}</div>
              <div className="text-[11px] text-muted-foreground truncate">{inst.city} · BLZ {inst.blz}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block text-sm">
      <div className="mb-1 text-xs font-medium text-muted-foreground">{label}</div>
      {children}
      {hint && <div className="text-[11px] text-muted-foreground mt-1">{hint}</div>}
    </label>
  );
}
