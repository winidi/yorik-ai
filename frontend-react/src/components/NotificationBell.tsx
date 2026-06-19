/**
 * Floating notification bell — top-right of every React route.
 *
 * Polls /api/notifications every 20s for the unread count (cheap),
 * and on click fetches the full list and renders them as a dropdown.
 * Click a row → mark read + navigate. "Mark all read" button at the
 * bottom of the dropdown.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import * as Popover from "@radix-ui/react-popover";
import { Bell, Check, CheckCheck, Loader2, ShieldAlert, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface Notification {
  id: number;
  kind: string;
  title: string;
  body?: string | null;
  payload?: Record<string, any> | null;
  navigate_to?: string | null;
  is_read: boolean;
  created_at: string;
}

interface ListResponse {
  notifications: Notification[];
  unread_count: number;
}

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(0);
  const [list, setList] = useState<Notification[] | null>(null);
  const [loading, setLoading] = useState(false);

  // Cheap count poll — every 20s regardless of dropdown state.
  const refreshCount = useCallback(async () => {
    try {
      const r = await api.get<ListResponse>("/api/notifications?unread_only=true&limit=1");
      setUnread(r.unread_count);
    } catch {}
  }, []);

  useEffect(() => {
    refreshCount();
    const t = setInterval(refreshCount, 20_000);
    return () => clearInterval(t);
  }, [refreshCount]);

  // Full list — fetched only when the dropdown opens.
  useEffect(() => {
    if (!open) return;
    let alive = true;
    setLoading(true);
    api.get<ListResponse>("/api/notifications?limit=20")
      .then(r => {
        if (alive) { setList(r.notifications); setUnread(r.unread_count); }
      })
      .catch(() => {})
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [open]);

  async function markRead(id: number) {
    try {
      await api.post(`/api/notifications/${id}/read`);
      setList(l => l ? l.map(n => n.id === id ? { ...n, is_read: true } : n) : l);
      setUnread(c => Math.max(0, c - 1));
    } catch {}
  }
  async function markAllRead() {
    try {
      await api.post("/api/notifications/mark-all-read");
      setList(l => l ? l.map(n => ({ ...n, is_read: true })) : l);
      setUnread(0);
    } catch {}
  }
  // For kind='email_proposal': one-click accept → the backend runs the
  // matching skill (add_bill / add_calendar_event) and marks the
  // notification read.
  const [busyId, setBusyId] = useState<number | null>(null);
  async function acceptProposal(n: Notification) {
    setBusyId(n.id);
    try {
      await api.post(`/api/notifications/${n.id}/accept`);
      setList(l => l ? l.filter(x => x.id !== n.id) : l);
      setUnread(c => Math.max(0, c - 1));
    } catch (e: any) {
      alert(`Couldn't accept: ${e?.message || e}`);
    } finally {
      setBusyId(null);
    }
  }

  // Spam confirm — when set, the spam panel expands inline below the
  // notification row instead of firing immediately. Lets the user pick
  // "also block the whole domain" before committing.
  const [spamConfirmId, setSpamConfirmId] = useState<number | null>(null);
  async function markSpam(n: Notification, blockDomain: boolean) {
    setBusyId(n.id);
    try {
      const r = await api.post<{
        ok: boolean;
        blocked: { kind: string; value: string }[];
        moved_to_junk: boolean;
      }>(`/api/notifications/${n.id}/spam`, { block_domain: blockDomain });
      setList(l => l ? l.filter(x => x.id !== n.id) : l);
      setUnread(c => Math.max(0, c - 1));
      setSpamConfirmId(null);
      // Lightweight inline feedback — no toast system in the bell yet.
      const blocks = r.blocked.map(b => b.value).join(" + ");
      const junkBit = r.moved_to_junk ? ", in Junk verschoben" : "";
      console.log(`[spam] blocked ${blocks}${junkBit}`);
    } catch (e: any) {
      alert(`Couldn't mark as spam: ${e?.message || e}`);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          className="fixed top-3 right-3 z-[60] w-10 h-10 rounded-full bg-card border border-border shadow-md hover:shadow-lg flex items-center justify-center text-muted-foreground hover:text-foreground transition"
          aria-label="Notifications"
          title={unread > 0 ? `${unread} unread notification${unread === 1 ? "" : "s"}` : "No new notifications"}
        >
          <Bell className="w-4 h-4" />
          {unread > 0 && (
            <span className="absolute -top-1 -right-1 min-w-[18px] h-[18px] px-1 rounded-full bg-red-500 text-white text-[10px] font-semibold flex items-center justify-center shadow">
              {unread > 99 ? "99+" : unread}
            </span>
          )}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          side="bottom" align="end" sideOffset={8}
          className="z-[900] w-[380px] max-h-[70vh] bg-card border border-border rounded-xl shadow-2xl overflow-hidden flex flex-col"
        >
          <div className="px-4 py-3 border-b border-border flex items-center justify-between">
            <h3 className="font-semibold text-sm">Notifications</h3>
            {list && list.some(n => !n.is_read) && (
              <button
                onClick={markAllRead}
                className="text-[11px] text-primary hover:underline flex items-center gap-1"
              >
                <CheckCheck className="w-3 h-3" />
                Mark all read
              </button>
            )}
          </div>
          <div className="flex-1 overflow-y-auto">
            {loading && !list && (
              <div className="p-8 text-center text-sm text-muted-foreground flex items-center justify-center gap-2">
                <Loader2 className="w-4 h-4 animate-spin" /> Loading…
              </div>
            )}
            {list && list.length === 0 && (
              <div className="p-8 text-center text-sm text-muted-foreground">
                <Bell className="w-8 h-8 mx-auto mb-2 opacity-30" />
                Nothing to see here.
              </div>
            )}
            {list?.map(n => {
              const isProposal = n.kind === "email_proposal";
              const proposalKind = n.payload?.category as ("bill" | "appointment" | undefined);
              return (
                <div
                  key={n.id}
                  className={cn(
                    "w-full text-left p-3 border-b border-border/40 transition flex gap-3",
                    !n.is_read && "bg-primary/[0.04]",
                    !isProposal && "hover:bg-muted/30 cursor-pointer",
                  )}
                  onClick={isProposal ? undefined : () => {
                    markRead(n.id);
                    if (n.navigate_to) {
                      setOpen(false);
                      window.location.assign(n.navigate_to);
                    }
                  }}
                >
                  <span className={cn(
                    "w-2 h-2 rounded-full mt-1.5 shrink-0",
                    n.is_read ? "bg-transparent" : "bg-primary",
                  )} />
                  <div className="flex-1 min-w-0">
                    <div className={cn("text-sm truncate", !n.is_read && "font-semibold")}>
                      {n.title}
                    </div>
                    {n.body && (
                      <div className="text-xs text-muted-foreground line-clamp-2 mt-0.5">{n.body}</div>
                    )}
                    <div className="text-[10px] text-muted-foreground mt-1">
                      {formatTime(n.created_at)}
                    </div>
                    {isProposal && spamConfirmId !== n.id && (
                      <div className="mt-2 flex flex-wrap gap-2 items-center">
                        <button
                          disabled={busyId === n.id}
                          onClick={(e) => { e.stopPropagation(); acceptProposal(n); }}
                          className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50"
                        >
                          {busyId === n.id
                            ? <Loader2 className="w-3 h-3 animate-spin" />
                            : <Check className="w-3 h-3" />}
                          Add to {proposalKind === "appointment" ? "calendar" : "bills"}
                        </button>
                        <button
                          disabled={busyId === n.id}
                          onClick={(e) => { e.stopPropagation(); markRead(n.id); setList(l => l ? l.filter(x => x.id !== n.id) : l); }}
                          className="flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-border hover:bg-muted/40 disabled:opacity-50"
                        >
                          <X className="w-3 h-3" /> Dismiss
                        </button>
                        {n.payload?.from_email && (
                          <button
                            disabled={busyId === n.id}
                            onClick={(e) => { e.stopPropagation(); setSpamConfirmId(n.id); }}
                            className="flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-rose-500/40 text-rose-500 hover:bg-rose-500/10 disabled:opacity-50"
                            title="Block this sender + move email to junk"
                          >
                            <ShieldAlert className="w-3 h-3" /> Spam
                          </button>
                        )}
                        {n.navigate_to && (
                          <button
                            onClick={(e) => { e.stopPropagation(); setOpen(false); window.location.assign(n.navigate_to!); }}
                            className="text-[11px] text-primary hover:underline ml-auto"
                          >
                            view email
                          </button>
                        )}
                      </div>
                    )}
                    {isProposal && spamConfirmId === n.id && (
                      <SpamConfirmPanel
                        senderEmail={n.payload?.from_email || ""}
                        busy={busyId === n.id}
                        onCancel={() => setSpamConfirmId(null)}
                        onConfirm={(blockDomain) => markSpam(n, blockDomain)}
                      />
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

// Inline spam-confirm panel — appears under a notification when the
// user clicks the Spam button. Shows the sender + a "block domain too"
// checkbox + confirm/cancel. Stops propagation everywhere so the parent
// notification's click handlers don't fire while the user is choosing.
function SpamConfirmPanel({
  senderEmail,
  busy,
  onConfirm,
  onCancel,
}: {
  senderEmail: string;
  busy: boolean;
  onConfirm: (blockDomain: boolean) => void;
  onCancel: () => void;
}) {
  const [blockDomain, setBlockDomain] = useState(false);
  const domain = senderEmail.includes("@") ? senderEmail.split("@")[1] : "";
  return (
    <div
      className="mt-2 p-2.5 rounded-md border border-rose-500/30 bg-rose-500/[0.04] space-y-2"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="text-[11px] text-foreground">
        Sender <span className="font-mono text-rose-600 dark:text-rose-400">{senderEmail || "(unknown)"}</span> blockieren
        und Mail in den Junk-Ordner verschieben?
      </div>
      {domain && (
        <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <input
            type="checkbox"
            checked={blockDomain}
            onChange={(e) => setBlockDomain(e.target.checked)}
            className="w-3 h-3"
          />
          Auch die ganze Domain <span className="font-mono">@{domain}</span> blockieren
        </label>
      )}
      <div className="flex gap-2">
        <button
          disabled={busy}
          onClick={() => onConfirm(blockDomain)}
          className="flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded bg-rose-500 text-white hover:opacity-90 disabled:opacity-50"
        >
          {busy
            ? <Loader2 className="w-3 h-3 animate-spin" />
            : <ShieldAlert className="w-3 h-3" />}
          Als Spam markieren
        </button>
        <button
          disabled={busy}
          onClick={onCancel}
          className="text-[11px] px-2 py-1 rounded border border-border hover:bg-muted/40 disabled:opacity-50"
        >
          Abbrechen
        </button>
      </div>
    </div>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso.replace(" ", "T") + (iso.includes("T") ? "" : "Z"));
  if (isNaN(d.getTime())) return iso;
  const now = new Date();
  const diffMin = (now.getTime() - d.getTime()) / 60_000;
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${Math.round(diffMin)} min ago`;
  if (diffMin < 24 * 60) return `${Math.round(diffMin / 60)}h ago`;
  return d.toLocaleDateString([], { day: "numeric", month: "short" });
}
