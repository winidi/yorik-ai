/**
 * Finance → Bills: what the household still has to pay.
 *
 * Open bills first (overdue in red, due within three days in amber),
 * paid ones folded below. A bill comes from a mail or a photographed
 * letter (accepted in the bell), from the chat, or from the form here.
 * "Paid" is a tap — or the bank: an open bill whose amount and payee
 * show up in a booking ticks itself off ("paid · bank").
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, ChevronDown, ChevronRight, ExternalLink, Landmark, Loader2, Plus, Trash2, X } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatDate, formatMoney, currency as householdCurrency } from "@/i18n/format";

export interface Bill {
  id: number;
  name: string;
  payee?: string | null;
  amount: number;
  currency: string;
  due_date: string;
  paid: boolean;
  paid_at?: string | null;
  paid_by?: "hand" | "bank" | null;
  recurring?: string | null;
  notes?: string | null;
  number?: string | null;
  source?: string | null;
  link?: string | null;
  days_left?: number | null;
  overdue?: boolean;
}

export function BillsTab() {
  const { t } = useTranslation();
  const [open, setOpen] = useState<Bill[] | null>(null);
  const [paid, setPaid] = useState<Bill[] | null>(null);
  const [showPaid, setShowPaid] = useState(false);
  const [adding, setAdding] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [o, p] = await Promise.all([
        api.get<Bill[]>("/api/bills?status=open"),
        api.get<Bill[]>("/api/bills?status=paid"),
      ]);
      setOpen(o);
      setPaid(p);
      setError(null);
    } catch (e: any) {
      setError(e?.message || String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    // The chat's add_bill / update_bill fire a refresh for this table.
    const onRefresh = (e: Event) => {
      const d = (e as CustomEvent).detail;
      if (d?.type === "refresh_data" && d.table === "bills") void load();
    };
    window.addEventListener("yorik-ui-action", onRefresh);
    return () => window.removeEventListener("yorik-ui-action", onRefresh);
  }, [load]);

  async function act(b: Bill, what: "paid" | "unpaid" | "delete") {
    setBusyId(b.id);
    try {
      if (what === "delete") await api.delete(`/api/bills/${b.id}`);
      else await api.post(`/api/bills/${b.id}/${what}`, {});
      await load();
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setBusyId(null);
    }
  }

  const totals = new Map<string, number>();
  for (const b of open || []) totals.set(b.currency, (totals.get(b.currency) || 0) + b.amount);
  const overdue = (open || []).filter(b => b.overdue).length;

  return (
    <section className="pt-2">
      <div className="flex items-start justify-between gap-4 mb-5">
        <div>
          <h2 className="text-[13px] font-medium text-muted-foreground mb-1">{t("finance.bills.heading")}</h2>
          {open && open.length > 0 ? (
            <div className="flex items-baseline gap-3 flex-wrap">
              {[...totals.entries()].map(([cur, sum]) => (
                <span key={cur} className="text-3xl font-semibold tabular-nums">{formatMoney(sum, cur)}</span>
              ))}
              <span className="text-sm text-muted-foreground">
                {t("finance.bills.openCount", { count: open.length })}
                {overdue > 0 && <span className="text-rose-500"> · {t("finance.bills.overdueCount", { count: overdue })}</span>}
              </span>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">{open === null ? "…" : t("finance.bills.none")}</p>
          )}
        </div>
        <button
          onClick={() => setAdding(a => !a)}
          className="shrink-0 flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted/50"
        >
          <Plus className="w-4 h-4" /> {t("finance.bills.add")}
        </button>
      </div>

      {error && <p className="text-xs text-rose-500 mb-3">{error}</p>}

      {adding && (
        <AddBillForm
          onClose={() => setAdding(false)}
          onSaved={() => { setAdding(false); void load(); }}
        />
      )}

      <div className="divide-y divide-border/60">
        {(open || []).map(b => (
          <BillRow key={b.id} bill={b} busy={busyId === b.id} onAct={act} />
        ))}
      </div>

      <p className="text-xs text-muted-foreground mt-5 leading-relaxed">
        {t("finance.bills.howItWorks")}
      </p>

      {paid && paid.length > 0 && (
        <div className="mt-8">
          <button
            onClick={() => setShowPaid(s => !s)}
            className="flex items-center gap-1 text-[13px] font-medium text-muted-foreground hover:text-foreground"
          >
            {showPaid ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            {t("finance.bills.paidHeading", { count: paid.length })}
          </button>
          {showPaid && (
            <div className="divide-y divide-border/60 mt-2 opacity-80">
              {paid.map(b => (
                <BillRow key={b.id} bill={b} busy={busyId === b.id} onAct={act} />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function BillRow({ bill: b, busy, onAct }: {
  bill: Bill; busy: boolean; onAct: (b: Bill, what: "paid" | "unpaid" | "delete") => void;
}) {
  const { t } = useTranslation();
  const due = b.due_date ? formatDate(b.due_date) : "";
  let when: string;
  let tone = "text-muted-foreground";
  if (b.paid) {
    when = b.paid_at ? t("finance.bills.paidOn", { date: formatDate(b.paid_at) }) : t("finance.bills.paid");
  } else if (b.overdue) {
    when = t("finance.bills.overdueSince", { date: due });
    tone = "text-rose-500";
  } else if (b.days_left === 0) {
    when = t("finance.bills.dueToday"); tone = "text-amber-500";
  } else if (b.days_left === 1) {
    when = t("finance.bills.dueTomorrow"); tone = "text-amber-500";
  } else if (b.days_left != null && b.days_left <= 3) {
    when = t("finance.bills.dueIn", { count: b.days_left }); tone = "text-amber-500";
  } else {
    when = t("finance.bills.dueOn", { date: due });
  }
  const who = b.payee && b.payee.toLowerCase() !== b.name.toLowerCase() ? b.payee : null;
  return (
    <div className="flex items-center gap-3 py-2.5 text-sm">
      <button
        onClick={() => onAct(b, b.paid ? "unpaid" : "paid")}
        disabled={busy}
        title={b.paid ? t("finance.bills.markUnpaid") : t("finance.bills.markPaid")}
        aria-label={b.paid ? t("finance.bills.markUnpaid") : t("finance.bills.markPaid")}
        className={cn(
          "w-6 h-6 rounded-full border grid place-items-center shrink-0 transition-colors",
          b.paid ? "bg-emerald-500 border-emerald-500 text-white" : "border-border hover:border-emerald-500",
        )}
      >
        {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : b.paid ? <Check className="w-3.5 h-3.5" /> : null}
      </button>
      <div className="min-w-0 flex-1">
        <div className={cn("truncate", b.paid && "line-through text-muted-foreground")}>
          {b.name}
          {who && <span className="text-muted-foreground"> · {who}</span>}
        </div>
        <div className={cn("text-xs flex items-center gap-1.5 flex-wrap", tone)}>
          <span>{when}</span>
          {b.paid && b.paid_by === "bank" && (
            <span className="inline-flex items-center gap-0.5 text-emerald-600"><Landmark className="w-3 h-3" /> {t("finance.bills.byBank")}</span>
          )}
          {b.recurring && <span className="text-muted-foreground">· {b.recurring}</span>}
          {b.link && (
            <a href={b.link} className="inline-flex items-center gap-0.5 text-primary hover:underline">
              <ExternalLink className="w-3 h-3" /> {b.source === "paperless" ? t("finance.bills.openDocument") : t("finance.bills.openEmail")}
            </a>
          )}
        </div>
      </div>
      <span className={cn("tabular-nums shrink-0", b.paid ? "text-muted-foreground" : "text-foreground")}>
        {formatMoney(b.amount, b.currency)}
      </span>
      <button
        onClick={() => onAct(b, "delete")}
        disabled={busy}
        title={t("finance.bills.delete")}
        aria-label={t("finance.bills.delete")}
        className="p-1.5 rounded-md text-muted-foreground/60 hover:text-rose-500 hover:bg-muted shrink-0"
      >
        <Trash2 className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}

function AddBillForm({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [amount, setAmount] = useState("");
  const [due, setDue] = useState("");
  const [payee, setPayee] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    const value = Number(amount.replace(",", "."));
    if (!name.trim() || !Number.isFinite(value) || value <= 0) {
      setErr(t("finance.bills.formInvalid"));
      return;
    }
    setSaving(true);
    try {
      await api.post("/api/bills", {
        name: name.trim(), amount: value, currency: householdCurrency(),
        due_date: due || null, payee: payee.trim() || null,
      });
      onSaved();
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setSaving(false);
    }
  }

  const field = "w-full rounded-md border border-border bg-background px-3 py-2 text-sm";
  return (
    <div className="rounded-xl border border-border bg-card p-4 mb-5">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label className="text-xs text-muted-foreground sm:col-span-2">
          {t("finance.bills.fieldName")}
          <input autoFocus value={name} onChange={e => setName(e.target.value)} className={cn(field, "mt-1")} placeholder={t("finance.bills.fieldNameHint")} />
        </label>
        <label className="text-xs text-muted-foreground">
          {t("finance.bills.fieldAmount", { currency: householdCurrency() })}
          <input inputMode="decimal" value={amount} onChange={e => setAmount(e.target.value)} className={cn(field, "mt-1")} placeholder="0,00" />
        </label>
        <label className="text-xs text-muted-foreground">
          {t("finance.bills.fieldDue")}
          <input type="date" value={due} onChange={e => setDue(e.target.value)} className={cn(field, "mt-1")} />
        </label>
        <label className="text-xs text-muted-foreground sm:col-span-2">
          {t("finance.bills.fieldPayee")}
          <input value={payee} onChange={e => setPayee(e.target.value)} className={cn(field, "mt-1")} placeholder={t("finance.bills.fieldPayeeHint")} />
        </label>
      </div>
      {err && <p className="text-xs text-rose-500 mt-2">{err}</p>}
      <div className="flex items-center justify-end gap-2 mt-4">
        <button onClick={onClose} className="px-3 py-1.5 text-sm rounded-md text-muted-foreground hover:bg-muted inline-flex items-center gap-1">
          <X className="w-4 h-4" /> {t("common.cancel")}
        </button>
        <button onClick={save} disabled={saving} className="px-3 py-1.5 text-sm rounded-md bg-primary text-primary-foreground font-medium inline-flex items-center gap-1 disabled:opacity-50">
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />} {t("common.save")}
        </button>
      </div>
    </div>
  );
}
