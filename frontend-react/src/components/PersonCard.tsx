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
import { useTranslation } from "react-i18next";
import { locale } from "@/i18n";

interface PersonData {
  identifier: string;
  contact?: { id: number; status?: string | null; display_name?: string | null } | null;
  channel?: { kind: string; value: string } | null;
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

/** "Save as contact" / "Confirm as contact" — the one step from a mail
 *  or a chat to a contact. Yorik never creates it by itself. */
function SaveAsContact({ data, onDone }: { data: PersonData; onDone: () => void }) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const c = data.contact;
  if (c && c.status !== "pending") {
    return <div className="text-2xs text-muted-foreground mt-1.5">{c.display_name && c.display_name !== data.primary_name ? t("contacts.person.inContactsAs", { name: c.display_name }) : t("contacts.person.inContacts")}</div>;
  }
  if (!data.channel) return null;
  async function run() {
    setBusy(true);
    try {
      if (c && c.status === "pending") {
        await api.post(`/api/contacts/${c.id}/promote`, {});
      } else {
        const created = await api.post<{ id: number }>("/api/contacts", { display_name: data.primary_name, kind: "person" });
        await api.post(`/api/contacts/${created.id}/channels`, { kind: data.channel!.kind, value: data.channel!.value });
      }
      onDone();
    } catch (e) {
      console.error("[person] save as contact failed", e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <button type="button" onClick={run} disabled={busy}
      className="mt-2 text-xs px-2 py-1 rounded-md border border-border hover:bg-muted disabled:opacity-50">
      {busy ? "…" : c ? t("contacts.person.confirmAsContact") : t("contacts.person.saveAsContact")}
    </button>
  );
}


function PersonContent({ identifier }: { identifier: string }) {
  const { t } = useTranslation();
  const [data, setData] = useState<PersonData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.get<PersonData>(`/api/people/${encodeURIComponent(identifier)}`)
      .then(d => { if (!cancelled) setData(d); })
      .catch(e => { if (!cancelled) setError(e.message || t("contacts.person.failed")); });
    return () => { cancelled = true; };
  }, [identifier, t]);

  if (error) return <div className="p-4 text-sm text-destructive">{error}</div>;
  if (!data) return (
    <div className="p-4 flex items-center gap-2 text-sm text-muted-foreground">
      <Loader2 className="w-4 h-4 animate-spin" /> {t("contacts.person.loading")}
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
          <div className="text-2xs text-muted-foreground mt-1.5">
            {t("contacts.person.alsoKnownAs", { names: data.names_used.filter(n => n !== data.primary_name).join(" · ") })}
          </div>
        )}
        <SaveAsContact data={data} onDone={() => { setData(null); api.get<PersonData>(`/api/people/${encodeURIComponent(identifier)}`).then(setData).catch(() => {}); }} />
      </div>

      {data.feed.length > 0 && (
        <Section title={t("contacts.person.recentContact")}>
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
                <span className="text-2xs text-muted-foreground tabular-nums shrink-0">
                  {formatWhen(f.ts)}
                </span>
              </div>
            );
          })}
        </Section>
      )}

      {data.documents.length > 0 && (
        <Section title={t("contacts.person.documents", { count: data.documents.length })}>
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
                {d.doc_date && <div className="text-2xs text-muted-foreground">{d.doc_date}</div>}
              </div>
            </a>
          ))}
        </Section>
      )}

      {data.events_recent.length > 0 && (
        <Section title={t("contacts.person.calendar")}>
          {data.events_recent.map(ev => (
            <div key={ev.id} className="flex items-start gap-2 py-1 text-xs">
              <Calendar className="w-3.5 h-3.5 shrink-0 mt-0.5 text-violet-500" />
              <div className="flex-1 min-w-0">
                <div className="truncate">{ev.title}</div>
                {ev.starts_at && (
                  <div className="text-2xs text-muted-foreground">{shortDate(ev.starts_at)}</div>
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
            {t("contacts.person.newEmail")}
          </a>
        )}
        {data.wa_chats[0] && (
          <a
            href={`/whatsapp?chat=${data.wa_chats[0].jid}`}
            className="flex-1 text-center py-1.5 rounded bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20"
          >
            <MessageSquare className="w-3 h-3 inline mr-1" />
            {t("contacts.person.openWa")}
          </a>
        )}
      </div>
    </>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="px-4 py-2 border-b border-border last:border-b-0">
      <div className="text-2xs text-muted-foreground font-semibold mb-1.5">
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
    return d.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
  }
  if (d.getFullYear() === now.getFullYear()) {
    return d.toLocaleDateString(locale(), { month: "short", day: "numeric" });
  }
  return d.toLocaleDateString(locale(), { year: "2-digit", month: "short" });
}

function shortDate(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(locale(), { month: "short", day: "numeric", year: "2-digit" }) +
    " " + d.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
}
