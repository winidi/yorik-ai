/**
 * Morning-briefing card for the Home screen.
 *
 * One bundled fetch to /api/dashboard/digest. Renders a compact
 * summary line (composed server-side so the wording is consistent
 * across surfaces) plus mini-rows of what's coming up. Each row is
 * clickable to jump into the relevant app.
 *
 * Polls every 5 minutes — frequent enough that adding a new bill via
 * the chat agent shows up "live" but cheap enough not to matter (the
 * digest is a single SQL aggregation, sub-100ms).
 */

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { createPortal } from "react-dom";
import { Calendar, FileText, CheckSquare, Mail, ArrowRight, Loader2, X, Check, ExternalLink, Image as ImageIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface DigestEvent {
  id: number;
  title: string;
  starts_at: string;
  color?: string | null;
}

interface DigestBill {
  id: number;
  name: string;
  amount: number;
  currency: string;
  due_date: string;
  recurring?: string | null;
  notes?: string | null;
  email_message_id?: number | null;
}

interface DigestTask {
  id: number;
  title: string;
  due_date?: string | null;
  person?: string | null;
}

interface DigestLabels {
  today: string;
  tomorrow: string;
  bills_this_week: string;
  tasks: string;
  unread: string;
  all_day: string;
  no_due_date: string;
  inbox_clear: string;
  photos_today: string;
}

interface DigestPhoto {
  id: string;
  thumbnail_url: string;
  original_name?: string;
  taken_at?: string;
}

interface Digest {
  language: string;
  title: string;
  greeting: string;
  summary: string;
  today_events: DigestEvent[];
  tomorrow_events: DigestEvent[];
  tomorrow_tasks: DigestTask[];
  bills_due_week: DigestBill[];
  priority_tasks: DigestTask[];
  photos_today: DigestPhoto[];
  unread_by_category: Record<string, number>;
  unread_total: number;
  labels: DigestLabels;
}

export function MorningBriefingCard() {
  const navigate = useNavigate();
  const [data, setData] = useState<Digest | null>(null);
  const [loading, setLoading] = useState(true);
  const [openBill, setOpenBill] = useState<DigestBill | null>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await api.get<Digest>("/api/dashboard/digest");
      setData(d);
    } catch {
      // Silent — the home screen still works without this card.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5 * 60 * 1000);
    return () => clearInterval(t);
  }, [refresh]);

  if (loading) {
    return (
      <div className="w-full bg-card border border-border rounded-2xl p-5 mb-6 flex items-center gap-3 text-muted-foreground">
        <Loader2 className="w-4 h-4 animate-spin" />
        <span className="text-sm">…</span>
      </div>
    );
  }
  if (!data) return null;

  // Hide entirely when there's genuinely nothing — first-run users
  // shouldn't see an empty briefing card screaming "you have 0 of
  // everything". The "inbox is clear" summary handles that gracefully
  // when there IS user data but nothing pressing.
  const isEmpty = !data.today_events.length
                && !data.tomorrow_events.length
                && !data.tomorrow_tasks.length
                && !data.bills_due_week.length
                && !data.priority_tasks.length
                && !data.photos_today.length
                && data.unread_total === 0;
  if (isEmpty) return null;

  // Helper: deep-link to /calendar at a specific date so clicks on
  // tomorrow's events don't dump the user on today. CalendarApp reads
  // the `?date=YYYY-MM-DD` query param on mount.
  const isoDate = (s: string) => (s || "").slice(0, 10);
  const goCalendar = (d?: string) => navigate(d ? `/calendar?date=${d}` : "/calendar");

  const L = data.labels;
  const tomorrowHasContent = data.tomorrow_events.length > 0 || data.tomorrow_tasks.length > 0;

  return (
    <div className="w-full bg-gradient-to-br from-violet-500/[0.06] to-blue-500/[0.06] border border-violet-500/20 rounded-2xl p-5 mb-6">
      <div className="flex items-baseline justify-between gap-3 mb-1">
        <div className="text-sm text-muted-foreground">{data.greeting}.</div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground/70 font-semibold">
          {data.title}
        </div>
      </div>
      <div className="text-base sm:text-lg font-medium leading-snug mb-4">
        {data.summary}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {data.today_events.length > 0 && (
          <BriefingSection
            icon={Calendar}
            tint="text-blue-500"
            title={L.today}
            onAll={() => goCalendar()}
          >
            {data.today_events.slice(0, 3).map(e => (
              <BriefingRow
                key={e.id}
                primary={e.title}
                secondary={(e.starts_at || "").slice(11, 16) || L.all_day}
                onClick={() => goCalendar(isoDate(e.starts_at))}
              />
            ))}
          </BriefingSection>
        )}

        {data.bills_due_week.length > 0 && (
          <BriefingSection
            icon={FileText}
            tint="text-amber-500"
            title={L.bills_this_week}
            onAll={() => navigate("/email")}
          >
            {data.bills_due_week.slice(0, 3).map(b => (
              <BriefingRow
                key={b.id}
                primary={b.name}
                secondary={`${b.amount.toFixed(2)} ${b.currency} · ${b.due_date}`}
                onClick={() => setOpenBill(b)}
              />
            ))}
          </BriefingSection>
        )}

        {data.priority_tasks.length > 0 && (
          <BriefingSection
            icon={CheckSquare}
            tint="text-emerald-500"
            title={L.tasks}
            onAll={() => navigate("/tasks")}
          >
            {data.priority_tasks.slice(0, 3).map(t => (
              <BriefingRow
                key={t.id}
                primary={t.title}
                secondary={t.due_date || L.no_due_date}
                onClick={() => navigate(`/tasks?task=${t.id}`)}
              />
            ))}
          </BriefingSection>
        )}

        {tomorrowHasContent && (
          <BriefingSection
            icon={Calendar}
            tint="text-violet-500"
            title={L.tomorrow}
            onAll={() => {
              // Tomorrow links to whichever app has content.
              if (data.tomorrow_events.length) {
                goCalendar(isoDate(data.tomorrow_events[0].starts_at));
              } else {
                navigate("/tasks");
              }
            }}
          >
            {data.tomorrow_events.slice(0, 2).map(e => (
              <BriefingRow
                key={`ev-${e.id}`}
                primary={e.title}
                secondary={(e.starts_at || "").slice(11, 16) || L.all_day}
                onClick={() => goCalendar(isoDate(e.starts_at))}
              />
            ))}
            {data.tomorrow_tasks.slice(0, 3 - Math.min(data.tomorrow_events.length, 2)).map(t => (
              <BriefingRow
                key={`tk-${t.id}`}
                primary={`✓ ${t.title}`}
                secondary={t.due_date || L.no_due_date}
                onClick={() => navigate(`/tasks?task=${t.id}`)}
              />
            ))}
          </BriefingSection>
        )}

        {data.unread_total > 0 && (
          <BriefingSection
            icon={Mail}
            tint="text-sky-500"
            title={`${data.unread_total} ${L.unread}`}
            onAll={() => navigate("/email")}
          >
            {Object.entries(data.unread_by_category)
              .sort(([, a], [, b]) => b - a)
              .slice(0, 3)
              .map(([cat, n]) => (
                <BriefingRow
                  key={cat}
                  primary={`${cat.charAt(0).toUpperCase()}${cat.slice(1)}`}
                  secondary={`${n} ${L.unread}`}
                  onClick={() => navigate(`/email?category=${encodeURIComponent(cat)}`)}
                />
              ))}
          </BriefingSection>
        )}
      </div>

      {data.photos_today.length > 0 && (
        <div className="mt-4 pt-4 border-t border-violet-500/15">
          <button
            onClick={() => navigate("/photos")}
            className="text-[10px] uppercase tracking-wider text-muted-foreground hover:text-foreground transition mb-2 inline-flex items-center gap-1.5"
          >
            <ImageIcon className="w-3 h-3" /> {L.photos_today}
            <span className="opacity-50">· {data.photos_today.length}</span>
          </button>
          <div className="grid grid-cols-6 gap-1.5">
            {data.photos_today.map(p => (
              <button
                key={p.id}
                onClick={() => navigate(`/photos?asset=${encodeURIComponent(p.id)}`)}
                title={p.original_name || ""}
                className="aspect-square overflow-hidden rounded-md border border-border bg-muted hover:border-violet-500/40 hover:shadow-md transition"
              >
                <img
                  src={p.thumbnail_url}
                  alt=""
                  loading="lazy"
                  className="w-full h-full object-cover"
                />
              </button>
            ))}
          </div>
        </div>
      )}

      {openBill && (
        <BillDetailModal
          bill={openBill}
          onClose={() => setOpenBill(null)}
          onPaid={async () => {
            try {
              await api.patch(`/api/bills/${openBill.id}`, { paid: true });
              setOpenBill(null);
              await refresh();
            } catch (e: any) {
              alert("Failed to mark paid: " + (e?.message || e));
            }
          }}
          onOpenEmail={openBill.email_message_id
            ? () => { setOpenBill(null); navigate(`/email?msg=${openBill.email_message_id}`); }
            : undefined}
        />
      )}
    </div>
  );
}

function BillDetailModal({ bill, onClose, onPaid, onOpenEmail }: {
  bill: DigestBill;
  onClose: () => void;
  onPaid: () => void;
  onOpenEmail?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  return createPortal(
    <div
      className="fixed inset-0 z-[800] flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-start justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className="w-10 h-10 rounded-full bg-amber-500/15 flex items-center justify-center shrink-0">
              <FileText className="w-5 h-5 text-amber-500" />
            </div>
            <div className="min-w-0">
              <div className="font-semibold leading-tight truncate" title={bill.name}>{bill.name}</div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground mt-0.5">Bill</div>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground hover:text-foreground transition" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="p-5 space-y-3 text-sm">
          <DetailRow label="Amount" value={`${bill.amount.toFixed(2)} ${bill.currency}`} valueClassName="text-base font-semibold tabular-nums" />
          <DetailRow label="Due" value={bill.due_date} />
          {bill.recurring && <DetailRow label="Recurring" value={bill.recurring} />}
          {bill.notes && <DetailRow label="Notes" value={bill.notes} />}
          {!bill.email_message_id && !bill.notes && (
            <div className="text-xs text-muted-foreground italic pt-1">
              Manually added or from seed data. No source email linked.
            </div>
          )}
        </div>

        <footer className="px-5 py-3 border-t border-border bg-muted/30 flex items-center gap-2 justify-end">
          {onOpenEmail && (
            <button
              onClick={onOpenEmail}
              className="px-3 py-1.5 rounded-md text-sm font-medium hover:bg-muted transition inline-flex items-center gap-1.5"
            >
              <ExternalLink className="w-3.5 h-3.5" />
              Open email
            </button>
          )}
          <button
            onClick={async () => { setBusy(true); try { await onPaid(); } finally { setBusy(false); } }}
            disabled={busy}
            className="px-3 py-1.5 rounded-md bg-emerald-500/90 hover:bg-emerald-500 text-white text-sm font-medium transition inline-flex items-center gap-1.5 disabled:opacity-50"
          >
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
            Mark paid
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  );
}

function DetailRow({ label, value, valueClassName }: {
  label: string; value: string; valueClassName?: string;
}) {
  return (
    <div className="flex items-baseline gap-3">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold w-20 shrink-0">{label}</div>
      <div className={cn("flex-1 break-words", valueClassName)}>{value}</div>
    </div>
  );
}

function BriefingSection({
  icon: Icon, tint, title, onAll, children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  tint: string;
  title: string;
  onAll?: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-card/60 border border-border/60 rounded-xl p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Icon className={cn("w-3.5 h-3.5", tint)} />
          <div className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold">{title}</div>
        </div>
        {onAll && (
          <button
            onClick={onAll}
            className="text-[11px] text-muted-foreground hover:text-foreground transition flex items-center gap-0.5"
          >
            open <ArrowRight className="w-3 h-3" />
          </button>
        )}
      </div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function BriefingRow({ primary, secondary, onClick }: { primary: string; secondary: string; onClick?: () => void }) {
  return (
    <button
      onClick={onClick}
      className="w-full text-left flex items-baseline justify-between gap-3 px-2 py-1 rounded-md hover:bg-muted/50 transition"
    >
      <span className="text-sm truncate min-w-0 flex-1">{primary}</span>
      <span className="text-[11px] text-muted-foreground tabular-nums shrink-0">{secondary}</span>
    </button>
  );
}
