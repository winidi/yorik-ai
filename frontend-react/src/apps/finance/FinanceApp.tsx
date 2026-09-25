/**
 * FinanceApp — connect a bank account (read-only, FinTS), see a
 * configurable Übersicht dashboard, browse categorised transactions
 * and detected recurring payments. The PIN field here is the ONLY
 * place it's ever typed; it goes straight to POST /api/bank/accounts
 * in the user's own browser, never through chat (see
 * docs/plans/2026-09-25-finanzen.md).
 */
import { useEffect, useRef, useState } from "react";
import { Landmark, Plus, RefreshCw, Trash2, X, Loader2, Search, Check, Pencil } from "lucide-react";
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

interface Recurring {
  account_id: number;
  counterparty: string;
  category: string | null;
  latest_amount: number;
  avg_amount: number;
  months_seen: number;
  last_date: string;
  avg_interval_days: number | null;
  next_expected: string | null;
}

// Mirrors backend/bank_sync.py's _LLM_CATEGORIES -- keep both lists in
// sync if you add a category, or the focus-category picker will offer
// choices the AI never actually assigns.
const ALL_CATEGORIES = [
  "Lebensmittel", "Wohnen", "Versicherung", "Telekommunikation",
  "Verträge & Abos", "Auto", "Freizeit", "Gesundheit", "Einkommen", "Sonstiges",
];

type Tab = "uebersicht" | "konten" | "umsaetze" | "vertraege";

function eur(n: number): string {
  return n.toLocaleString("de-DE", { style: "currency", currency: "EUR" });
}

function fmtDate(iso: string): string {
  return new Date(iso + "T00:00:00").toLocaleDateString("de-DE", { day: "numeric", month: "short" });
}

function dateHeading(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("de-DE", { weekday: "short", day: "numeric", month: "short" });
}

export function FinanceApp() {
  const [accounts, setAccounts] = useState<BankAccount[]>([]);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [recurring, setRecurring] = useState<Recurring[]>([]);
  const [focusCategories, setFocusCategories] = useState<string[]>(["Lebensmittel", "Verträge & Abos"]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [days, setDays] = useState(30);
  const [syncingId, setSyncingId] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("uebersicht");
  const [selectedAccount, setSelectedAccount] = useState<number | "all">("all");
  const [editingFocus, setEditingFocus] = useState(false);
  const [filterCategory, setFilterCategory] = useState<string | null>(null);

  function jumpToCategory(cat: string) {
    setTab("umsaetze");
    setFilterCategory(cat);
  }

  async function refresh() {
    setLoading(true);
    try {
      const acctQuery = selectedAccount === "all" ? "" : `&account_id=${selectedAccount}`;
      const [accs, txs, rec] = await Promise.all([
        api.get<BankAccount[]>("/api/bank/accounts"),
        // Postgres NUMERIC comes over the wire as a JSON string (to
        // avoid float precision loss), not a number — treating it as
        // one turned category totals into concatenated digit soup
        // ("0-388.21-425.22...") instead of a sum. Parse once, here.
        api.get<Array<Omit<Transaction, "amount"> & { amount: string }>>(
          `/api/bank/transactions?days=${days}${acctQuery}`,
        ),
        api.get<Recurring[]>(`/api/bank/recurring?${selectedAccount === "all" ? "" : `account_id=${selectedAccount}`}`),
      ]);
      setAccounts(accs);
      setTransactions(txs.map(t => ({ ...t, amount: Number(t.amount) })));
      setRecurring(rec);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [days, selectedAccount]);

  useEffect(() => {
    api.get<{ categories: string[] }>("/api/bank/focus-categories")
      .then(r => setFocusCategories(r.categories))
      .catch(() => {});
  }, []);

  async function saveFocusCategories(cats: string[]) {
    const res = await api.put<{ categories: string[] }>("/api/bank/focus-categories", { categories: cats });
    setFocusCategories(res.categories);
    setEditingFocus(false);
  }

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
  const maxAbs = Math.max(1, ...categoryRows.map(([, v]) => Math.abs(v)));
  const totalOut = transactions.filter(t => t.amount < 0).reduce((s, t) => s + t.amount, 0);
  const totalIn = transactions.filter(t => t.amount > 0).reduce((s, t) => s + t.amount, 0);

  // Group consecutive transactions by day so a date is a heading, not
  // a repeated label on every row (three "Landkreis Peine" entries in
  // a row all said "2026-09-25" three times). A category total on its
  // own doesn't say what it's made of -- clicking one filters this list
  // down to exactly the transactions that add up to it.
  const filteredTransactions = filterCategory
    ? transactions.filter(t => (t.category || "unkategorisiert") === filterCategory)
    : transactions;
  const txByDay: Array<[string, Transaction[]]> = [];
  for (const t of filteredTransactions) {
    const last = txByDay[txByDay.length - 1];
    if (last && last[0] === t.booking_date) last[1].push(t);
    else txByDay.push([t.booking_date, [t]]);
  }

  const hasAccounts = accounts.length > 0;
  const showSidebar = hasAccounts && (tab === "uebersicht" || tab === "umsaetze");

  return (
    <div className="h-screen overflow-y-auto bg-background">
    <div className="px-6 pt-6 pb-24 max-w-2xl lg:max-w-4xl mx-auto w-full">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-2.5">
          <Landmark className="w-5 h-5 text-muted-foreground" />
          <h1 className="text-lg font-semibold">Finance</h1>
        </div>
        <button
          onClick={() => setShowForm(true)}
          className="flex items-center gap-1.5 rounded-md bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium"
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

      {hasAccounts && (
        <>
          <div className="flex border-b border-border mb-6">
            {([
              ["uebersicht", "Übersicht"], ["konten", "Konten"],
              ["umsaetze", "Umsätze"], ["vertraege", "Verträge"],
            ] as const).map(([id, label]) => (
              <button
                key={id}
                onClick={() => setTab(id)}
                className={cn(
                  "px-3.5 py-2 text-sm border-b-2 -mb-px transition-colors",
                  tab === id
                    ? "border-foreground font-medium text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                {label}
              </button>
            ))}
          </div>

          {accounts.length > 1 && (
            <div className="flex items-center gap-1.5 flex-wrap mb-8">
              <button
                onClick={() => setSelectedAccount("all")}
                className={cn(
                  "rounded-full px-3 py-1 text-[13px] border transition-colors",
                  selectedAccount === "all"
                    ? "bg-foreground text-background border-foreground"
                    : "border-border text-muted-foreground hover:text-foreground",
                )}
              >
                Alle Konten
              </button>
              {accounts.map(a => (
                <button
                  key={a.id}
                  onClick={() => setSelectedAccount(a.id)}
                  className={cn(
                    "rounded-full px-3 py-1 text-[13px] border transition-colors",
                    selectedAccount === a.id
                      ? "bg-foreground text-background border-foreground"
                      : "border-border text-muted-foreground hover:text-foreground",
                  )}
                >
                  {a.display_name}
                </button>
              ))}
            </div>
          )}
        </>
      )}

      {!hasAccounts && !loading && (
        <p className="text-sm text-muted-foreground pt-8">
          Noch kein Konto verbunden. Nur lesend — Yorik kann nichts überweisen.
        </p>
      )}

      <div className={cn(showSidebar && "lg:grid lg:grid-cols-[1fr_20rem] lg:gap-10 lg:items-start")}>
        <div className="min-w-0">
          {tab === "uebersicht" && hasAccounts && (
            <>
              <div className={cn("mb-9", accounts.length <= 1 && "pt-2")}>
                <div className="flex items-end justify-between mb-1">
                  <div className="flex flex-col sm:flex-row sm:items-baseline gap-4 sm:gap-10">
                    <div>
                      <div className="text-[13px] text-muted-foreground mb-1">Einnahmen</div>
                      <div className="text-4xl font-semibold tabular-nums text-emerald-600">{eur(totalIn)}</div>
                    </div>
                    <div>
                      <div className="text-[13px] text-muted-foreground mb-1">Ausgaben</div>
                      <div className="text-4xl font-semibold tabular-nums text-rose-500">{eur(totalOut)}</div>
                    </div>
                  </div>
                  <PeriodSelect days={days} onChange={setDays} />
                </div>
              </div>

              <QuickStats
                byCategory={byCategory}
                focusCategories={focusCategories}
                editing={editingFocus}
                onEdit={() => setEditingFocus(true)}
                onCancel={() => setEditingFocus(false)}
                onSave={saveFocusCategories}
                onCategoryClick={jumpToCategory}
              />

              <RecurringSection
                items={recurring}
                limit={3}
                onShowAll={() => setTab("vertraege")}
              />
            </>
          )}

          {tab === "konten" && (
            <AccountsList accounts={accounts} syncingId={syncingId} onSync={handleSync} onDelete={handleDelete} />
          )}

          {tab === "umsaetze" && hasAccounts && (
            <>
              <section className="mb-9">
                <div className="flex items-center justify-between mb-3">
                  <h2 className="text-[13px] font-medium text-muted-foreground">Nach Kategorie</h2>
                  <PeriodSelect days={days} onChange={setDays} />
                </div>
                {categoryRows.length === 0 && <p className="text-sm text-muted-foreground">Noch keine Umsätze.</p>}
                <div className="space-y-1">
                  {categoryRows.map(([cat, total]) => (
                    <button
                      key={cat}
                      onClick={() => setFilterCategory(f => f === cat ? null : cat)}
                      className={cn(
                        "block w-full text-left rounded-md px-2 -mx-2 py-1.5 transition-colors",
                        filterCategory === cat ? "bg-muted" : "hover:bg-muted/50",
                      )}
                      title="Klicken, um die zugehörigen Umsätze zu sehen"
                    >
                      <div className="flex items-baseline justify-between text-sm mb-1">
                        <span className={cn(cat === "unkategorisiert" && "text-muted-foreground italic")}>{cat}</span>
                        <span className="tabular-nums">{eur(total)}</span>
                      </div>
                      <div className="h-1 rounded-full bg-muted overflow-hidden">
                        <div
                          className={cn("h-full rounded-full", total < 0 ? "bg-rose-500/70" : "bg-emerald-500/70")}
                          style={{ width: `${Math.max(3, (Math.abs(total) / maxAbs) * 100)}%` }}
                        />
                      </div>
                    </button>
                  ))}
                </div>
              </section>

              <section>
                <div className="flex items-center gap-2 mb-3">
                  <h2 className="text-[13px] font-medium text-muted-foreground">Umsätze</h2>
                  {filterCategory && (
                    <button
                      onClick={() => setFilterCategory(null)}
                      className="flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[11px] text-foreground hover:bg-muted/70"
                    >
                      {filterCategory} <X className="w-3 h-3" />
                    </button>
                  )}
                </div>
                {filteredTransactions.length === 0 && (
                  <p className="text-sm text-muted-foreground">Keine Umsätze in dieser Kategorie im gewählten Zeitraum.</p>
                )}
                {txByDay.map(([day, txs]) => (
                  <div key={day} className="mb-4">
                    <div className="text-[11px] font-medium text-muted-foreground/80 mb-1.5">{dateHeading(day)}</div>
                    <div className="divide-y divide-border/60">
                      {txs.map(t => (
                        <div key={t.id} className="flex items-center justify-between text-sm py-1.5">
                          <div className="min-w-0 flex-1">
                            <div className="truncate">{t.counterparty || t.purpose || "—"}</div>
                            {t.category && <div className="text-[11px] text-muted-foreground truncate">{t.category}</div>}
                          </div>
                          <span className={cn("tabular-nums shrink-0 ml-3", t.amount < 0 ? "text-rose-500" : "text-emerald-600")}>
                            {eur(t.amount)}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </section>
            </>
          )}

          {tab === "vertraege" && (
            <section className="pt-2">
              <h2 className="text-[13px] font-medium text-muted-foreground mb-1">Verträge & Abos</h2>
              <p className="text-[12px] text-muted-foreground mb-5">
                Automatisch erkannt: gleicher Empfänger, ähnlicher Betrag, in mindestens zwei
                verschiedenen Monaten der letzten 6 Monate.
              </p>
              <RecurringSection items={recurring} hideHeading />
            </section>
          )}
        </div>

        {showSidebar && (
          <aside className="hidden lg:block pt-2 space-y-9">
            <div>
              <h2 className="text-[13px] font-medium text-muted-foreground mb-3">Konten</h2>
              <AccountsList accounts={accounts} syncingId={syncingId} onSync={handleSync} onDelete={handleDelete} compact />
            </div>
            {tab === "umsaetze" && (
              <div>
                <h2 className="text-[13px] font-medium text-muted-foreground mb-3">Verträge & Abos</h2>
                <RecurringSection items={recurring} limit={4} onShowAll={() => setTab("vertraege")} compact />
              </div>
            )}
          </aside>
        )}
      </div>
    </div>
    <Dock activeAppId="finance" />
    </div>
  );
}

function PeriodSelect({ days, onChange }: { days: number; onChange: (n: number) => void }) {
  return (
    <select
      value={days}
      onChange={e => onChange(Number(e.target.value))}
      className="text-xs rounded-md border border-border bg-background px-2 py-1 shrink-0"
    >
      <option value={30}>30 Tage</option>
      <option value={90}>90 Tage</option>
      <option value={180}>180 Tage</option>
    </select>
  );
}

function AccountsList({ accounts, syncingId, onSync, onDelete, compact }: {
  accounts: BankAccount[]; syncingId: number | null;
  onSync: (id: number) => void; onDelete: (id: number) => void; compact?: boolean;
}) {
  return (
    <div className="divide-y divide-border">
      {accounts.map(a => (
        <div
          key={a.id}
          className={cn(
            "group flex items-center gap-3 py-2.5 pl-3 border-l-2",
            a.space_id ? "border-l-teal-500" : "border-l-transparent",
          )}
        >
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-1.5">
              <span className={cn("font-medium truncate", compact && "text-sm")}>{a.display_name}</span>
              <span className="text-[11px] text-muted-foreground shrink-0">
                {a.space_id ? "geteilt" : "privat"}
              </span>
            </div>
            {!compact && (
              <div className="text-[12px] text-muted-foreground truncate">
                {a.iban || "IBAN noch nicht bekannt"}
              </div>
            )}
            {a.last_sync_error ? (
              <div className="text-[12px] text-rose-500 mt-0.5 truncate">Sync-Fehler: {a.last_sync_error}</div>
            ) : (!compact && a.last_synced_at) ? (
              <div className="text-[11px] text-muted-foreground/80 mt-0.5">Synchronisiert: {a.last_synced_at}</div>
            ) : null}
          </div>
          <button
            onClick={() => onSync(a.id)}
            disabled={syncingId === a.id}
            className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted opacity-0 group-hover:opacity-100 focus-visible:opacity-100 disabled:opacity-50 transition-opacity"
            title="Jetzt synchronisieren"
          >
            {syncingId === a.id ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          </button>
          {!compact && (
            <button
              onClick={() => onDelete(a.id)}
              className="p-1.5 rounded-md text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity"
              title="Konto entfernen"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

// Quickstats: the "configurable" part of the dashboard -- which two
// categories get pinned here is a per-user preference (GET/PUT
// /api/bank/focus-categories), not hardcoded, so "Lebensmittel" and
// "Verträge & Abos" are just the defaults, not the only options.
function QuickStats({ byCategory, focusCategories, editing, onEdit, onCancel, onSave, onCategoryClick }: {
  byCategory: Map<string, number>; focusCategories: string[]; editing: boolean;
  onEdit: () => void; onCancel: () => void; onSave: (cats: string[]) => void;
  onCategoryClick: (cat: string) => void;
}) {
  const [draft, setDraft] = useState(focusCategories);
  useEffect(() => { setDraft(focusCategories); }, [focusCategories, editing]);

  if (editing) {
    return (
      <div className="border-y border-border py-4 mb-9 space-y-3">
        <div className="text-[13px] font-medium text-muted-foreground">Im Blick — auswählen</div>
        <div className="flex flex-col sm:flex-row gap-2">
          {[0, 1].map(i => (
            <select
              key={i}
              value={draft[i] || ALL_CATEGORIES[0]}
              onChange={e => setDraft(d => { const next = [...d]; next[i] = e.target.value; return next; })}
              className="text-sm rounded-md border border-border bg-background px-2 py-1.5 flex-1"
            >
              {ALL_CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          ))}
        </div>
        <div className="flex gap-2">
          <button onClick={() => onSave(draft)} className="rounded-md bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium">
            Speichern
          </button>
          <button onClick={onCancel} className="rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground">
            Abbrechen
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="relative border-y border-border mb-9">
      <div className="grid grid-cols-2 divide-x divide-border">
        {focusCategories.slice(0, 2).map(cat => (
          <button
            key={cat}
            onClick={() => onCategoryClick(cat)}
            className="text-left py-4 first:pr-4 last:pl-4 hover:bg-muted/50 transition-colors rounded-md"
            title="Klicken, um die zugehörigen Umsätze zu sehen"
          >
            <div className="text-[13px] text-muted-foreground mb-1 truncate">{cat}</div>
            <div className="text-xl font-semibold tabular-nums">{eur(Math.abs(byCategory.get(cat) || 0))}</div>
          </button>
        ))}
      </div>
      <button
        onClick={onEdit}
        className="absolute -top-3 right-0 p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted"
        title="Andere Kategorien auswählen"
      >
        <Pencil className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}

function RecurringSection({ items, limit, onShowAll, compact, hideHeading }: {
  items: Recurring[]; limit?: number; onShowAll?: () => void; compact?: boolean; hideHeading?: boolean;
}) {
  const shown = limit ? items.slice(0, limit) : items;
  if (shown.length === 0) {
    return <p className="text-sm text-muted-foreground">Noch keine wiederkehrenden Zahlungen erkannt.</p>;
  }
  return (
    <div className={cn(!compact && "mb-9")}>
      {!compact && !hideHeading && <h2 className="text-[13px] font-medium text-muted-foreground mb-3">Verträge & Abos</h2>}
      <div className="divide-y divide-border">
        {shown.map(r => (
          <div key={`${r.account_id}-${r.counterparty}`} className="flex items-center gap-3 py-2.5 pl-3 border-l-2 border-l-amber-500">
            <div className="min-w-0 flex-1">
              <div className={cn("font-medium truncate", compact && "text-sm")}>{r.counterparty}</div>
              <div className="text-[11px] text-muted-foreground truncate">
                {r.category || "unkategorisiert"}
                {r.next_expected && !compact && ` · ca. ${fmtDate(r.next_expected)}`}
              </div>
            </div>
            <span className="tabular-nums shrink-0 text-sm text-rose-500">{eur(r.avg_amount)}</span>
          </div>
        ))}
      </div>
      {limit && items.length > limit && onShowAll && (
        <button onClick={onShowAll} className="text-[13px] text-muted-foreground hover:text-foreground mt-2">
          Alle {items.length} anzeigen →
        </button>
      )}
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
