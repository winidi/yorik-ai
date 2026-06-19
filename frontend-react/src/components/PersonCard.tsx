/**
 * Hover popover showing a person's cross-channel context.
 *
 * Wrap any sender avatar / name with <PersonHover identifier="alex@…">
 * to get the Apple-Mail-style "hover to see everything about this
 * person" affordance. Renders via Radix Popover (already in deps) so
 * positioning + escape handling + click-outside are handled.
 */

import * as Popover from "@radix-ui/react-popover";
import { useEffect, useRef, useState } from "react";
import { Mail, MessageSquare, Calendar, FileText, Loader2, Phone } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

interface PersonData {
  identifier: string;
  primary_name: string;
  primary_email?: string | null;
  primary_phone?: string | null;
  names_used: string[];
  emails_recent: Array<{
    id: number; subject: string; snippet?: string;
    date_received?: string; is_sent?: boolean;
    from_name?: string; from_email?: string;
  }>;
  wa_chats: Array<{ jid: string; name?: string; last_message_ts?: number }>;
  wa_messages_recent: Array<{
    msg_id: string; chat_jid: string; text?: string;
    transcript?: string; timestamp: number; from_me?: boolean;
  }>;
  events_recent: Array<{
    id: number; title: string; starts_at?: string; person?: string;
  }>;
  documents: Array<{
    paperless_doc_id: number; doc_title: string;
    doc_url?: string; doc_date?: string;
  }>;
  feed: Array<{
    kind: "email" | "wa" | "event"; ts?: number;
    label: string; snippet?: string; ref: any;
  }>;
}

const HOVER_DELAY_MS = 400;
const CLOSE_DELAY_MS = 200;

interface Props {
  identifier: string;
  children: React.ReactNode;
}

export function PersonHover({ identifier, children }: Props) {
  const [open, setOpen] = useState(false);
  const hoverTimer = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);

  const enter = () => {
    if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null; }
    if (open) return;
    hoverTimer.current = window.setTimeout(() => setOpen(true), HOVER_DELAY_MS);
  };
  const leave = () => {
    if (hoverTimer.current) { clearTimeout(hoverTimer.current); hoverTimer.current = null; }
    closeTimer.current = window.setTimeout(() => setOpen(false), CLOSE_DELAY_MS);
  };
  useEffect(() => () => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    if (closeTimer.current) clearTimeout(closeTimer.current);
  }, []);

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <span onMouseEnter={enter} onMouseLeave={leave} className="inline-block">
          {children}
        </span>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          side="right"
          align="start"
          sideOffset={8}
          className="z-[900] w-[380px] bg-card border border-border rounded-xl shadow-2xl p-0 overflow-hidden"
          onMouseEnter={enter}
          onMouseLeave={leave}
        >
          <PersonContent identifier={identifier} />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function PersonContent({ identifier }: { identifier: string }) {
  const [data, setData] = useState<PersonData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.get<PersonData>(`/api/people/${encodeURIComponent(identifier)}`)
      .then(d => { if (!cancelled) setData(d); })
      .catch(e => { if (!cancelled) setError(e.message || "failed"); });
    return () => { cancelled = true; };
  }, [identifier]);

  if (error) return <div className="p-4 text-sm text-destructive">{error}</div>;
  if (!data) return (
    <div className="p-4 flex items-center gap-2 text-sm text-muted-foreground">
      <Loader2 className="w-4 h-4 animate-spin" /> Loading…
    </div>
  );

  return (
    <>
      <div className="p-4 border-b border-border">
        <div className="font-semibold text-base">{data.primary_name}</div>
        {data.primary_email && (
          <div className="text-xs text-muted-foreground truncate flex items-center gap-1.5 mt-0.5">
            <Mail className="w-3 h-3 shrink-0" /> {data.primary_email}
          </div>
        )}
        {data.primary_phone && (
          <div className="text-xs text-muted-foreground truncate flex items-center gap-1.5 mt-0.5">
            <Phone className="w-3 h-3 shrink-0" /> {data.primary_phone}
          </div>
        )}
        {data.names_used.length > 1 && (
          <div className="text-[10px] text-muted-foreground mt-1.5">
            also known as: {data.names_used.filter(n => n !== data.primary_name).join(" · ")}
          </div>
        )}
      </div>

      {data.feed.length > 0 && (
        <Section title="Recent contact">
          {data.feed.map((f, i) => {
            const Icon = f.kind === "email" ? Mail
                        : f.kind === "wa" ? MessageSquare
                        : Calendar;
            return (
              <div key={i} className="flex items-start gap-2 py-1.5 text-xs">
                <Icon className="w-3.5 h-3.5 shrink-0 mt-0.5 text-muted-foreground" />
                <div className="flex-1 min-w-0">
                  <div className="truncate">{f.label}</div>
                </div>
                <span className="text-[10px] text-muted-foreground tabular-nums shrink-0">
                  {formatWhen(f.ts)}
                </span>
              </div>
            );
          })}
        </Section>
      )}

      {data.documents.length > 0 && (
        <Section title={`Documents · ${data.documents.length}`}>
          {data.documents.map((d, i) => (
            <a
              key={i}
              href={d.doc_url || "#"}
              target="_blank" rel="noreferrer"
              className="flex items-start gap-2 py-1 text-xs hover:bg-muted/40 -mx-1 px-1 rounded"
            >
              <FileText className="w-3.5 h-3.5 shrink-0 mt-0.5 text-amber-500" />
              <div className="flex-1 min-w-0">
                <div className="truncate">{d.doc_title}</div>
                {d.doc_date && <div className="text-[10px] text-muted-foreground">{d.doc_date}</div>}
              </div>
            </a>
          ))}
        </Section>
      )}

      {data.events_recent.length > 0 && (
        <Section title="Calendar">
          {data.events_recent.map(ev => (
            <div key={ev.id} className="flex items-start gap-2 py-1 text-xs">
              <Calendar className="w-3.5 h-3.5 shrink-0 mt-0.5 text-violet-500" />
              <div className="flex-1 min-w-0">
                <div className="truncate">{ev.title}</div>
                {ev.starts_at && (
                  <div className="text-[10px] text-muted-foreground">{shortDate(ev.starts_at)}</div>
                )}
              </div>
            </div>
          ))}
        </Section>
      )}

      <div className="p-3 border-t border-border flex gap-2 text-xs">
        {data.primary_email && (
          <a
            href={`/r/email`}
            className="flex-1 text-center py-1.5 rounded bg-primary/10 text-primary hover:bg-primary/20"
          >
            <Mail className="w-3 h-3 inline mr-1" />
            New email
          </a>
        )}
        {data.wa_chats[0] && (
          <a
            href={`/whatsapp?chat=${data.wa_chats[0].jid}`}
            className="flex-1 text-center py-1.5 rounded bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20"
          >
            <MessageSquare className="w-3 h-3 inline mr-1" />
            Open WA
          </a>
        )}
      </div>
    </>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="px-4 py-2 border-b border-border last:border-b-0">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-1.5">
        {title}
      </div>
      <div className="space-y-0">{children}</div>
    </div>
  );
}

function formatWhen(ts?: number): string {
  if (!ts) return "";
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  if (d.getFullYear() === now.getFullYear()) {
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  return d.toLocaleDateString([], { year: "2-digit", month: "short" });
}

function shortDate(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: "2-digit" }) +
    " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
