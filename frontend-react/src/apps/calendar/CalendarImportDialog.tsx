/**
 * Bring an existing calendar (Google, iCloud, Outlook, Nextcloud) into
 * Yorik: import an .ics file once, or subscribe to a secret iCal
 * address as a read-only mirror that follows its source. One direction
 * only — nothing goes from Yorik to the other side.
 * Backend: backend/calendar_import.py.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { CloudDownload, ExternalLink, FileUp, Loader2, RefreshCw, Trash2, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import i18n from "@/i18n";
import { formatDate } from "@/i18n/format";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { Calendar } from "./types";

interface Feed { id: number; calendar_id: number; url_host: string; last_sync_at: string | null; last_status: string | null; last_error: string | null; event_count: number }
interface Preview { events: number; series: number; first: string | null; last: string | null }

// Where each service hides its secret calendar address, with a button
// straight to that settings page. Labels and steps are catalogue keys
// (calendar.import.source.<id>.*), shown in the person's language.
type SourceId = "google" | "icloud" | "outlook" | "other";
const SOURCES: Record<SourceId, { label: string; steps: number; url?: string }> = {
  google:  { label: "Google", url: "https://calendar.google.com/calendar/r/settings", steps: 2 },
  icloud:  { label: "iPhone / iCloud", steps: 2 },
  outlook: { label: "Outlook", url: "https://outlook.live.com/calendar/0/options/calendar/SharedCalendars", steps: 2 },
  other:   { get label() { return i18n.t("calendar.import.source.other.label"); }, steps: 1 },
};

export function CalendarImportDialog({ calendars, onClose, onChanged }: {
  calendars: Calendar[]; onClose: () => void; onChanged: () => void;
}) {
  const { t } = useTranslation();
  const mine = calendars.filter(c => c.you_own && c.kind === "personal" && !c.read_only);
  const [tab, setTab] = useState<"subscribe" | "file">("subscribe");
  const [feeds, setFeeds] = useState<Feed[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  // subscribe
  const [url, setUrl] = useState("");
  const [source, setSource] = useState<SourceId>("google");
  const [name, setName] = useState("Google");
  // file
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [target, setTarget] = useState<number | null>(mine[0]?.id ?? null);
  const [preview, setPreview] = useState<Preview | null>(null);

  const loadFeeds = useCallback(async () => {
    try { setFeeds(await api.get<Feed[]>("/api/calendar-import/feeds")); } catch { /* none */ }
  }, []);
  useEffect(() => { loadFeeds(); }, [loadFeeds]);

  async function subscribe() {
    setBusy("subscribe"); setError(null); setDone(null);
    try {
      // iPhone/iCloud share links start with webcal://; it's the same address over https.
      const clean = url.trim().replace(/^webcals?:\/\//i, "https://");
      const r = await api.post<{ sync: { added: number } }>("/api/calendar-import/feeds", { url: clean, name: name.trim() || SOURCES[source].label });
      setDone(t("calendar.import.subscribedDone", { count: r.sync.added }));
      setUrl(""); await loadFeeds(); onChanged();
    } catch (e: any) { setError(e?.message || t("calendar.import.failed")); } finally { setBusy(null); }
  }
  async function syncNow(f: Feed) {
    setBusy(`sync-${f.id}`); setError(null);
    try { await api.post(`/api/calendar-import/feeds/${f.id}/sync`, {}); await loadFeeds(); onChanged(); }
    catch (e: any) { setError(e?.message || t("calendar.import.syncFailed")); } finally { setBusy(null); }
  }
  async function unsubscribe(f: Feed) {
    if (!confirm(t("calendar.import.confirmUnsubscribe"))) return;
    setBusy(`del-${f.id}`);
    try { await api.delete(`/api/calendar-import/feeds/${f.id}`); await loadFeeds(); onChanged(); } finally { setBusy(null); }
  }

  async function sendFile(dryRun: boolean) {
    if (!file || !target) return;
    setBusy(dryRun ? "preview" : "import"); setError(null); setDone(null);
    try {
      const fd = new FormData(); fd.append("file", file);
      const r = await fetch(`/api/calendar-import/file?calendar_id=${target}${dryRun ? "&dry_run=1" : ""}`, { method: "POST", credentials: "include", body: fd });
      const j = await r.json().catch(() => ({} as any));
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      if (dryRun) setPreview(j);
      else { setDone(t("calendar.import.importedDone", { added: j.added, updated: j.updated, unchanged: j.unchanged })); setPreview(null); setFile(null); onChanged(); }
    } catch (e: any) { setError(e?.message || t("calendar.import.importFailed")); } finally { setBusy(null); }
  }

  const nameOf = (id: number) => calendars.find(c => c.id === id)?.name || t("calendar.import.calendarFallback");
  const day = (iso: string | null) => iso ? formatDate(iso) : "";

  return (
    <div className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4" onClick={onClose}>
      <div className="w-full max-w-lg rounded-xl border border-border bg-card text-card-foreground shadow-xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 h-14 border-b border-border">
          <div className="font-semibold text-sm">{t("calendar.import.title")}</div>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground"><X className="w-4 h-4" /></button>
        </div>
        <div className="px-5 pt-4 flex gap-1">
          {([["subscribe", t("calendar.import.tabSubscribe"), CloudDownload], ["file", t("calendar.import.tabFile"), FileUp]] as const).map(([id, label, Icon]) => (
            <button key={id} onClick={() => { setTab(id); setError(null); setDone(null); }}
                    className={cn("flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium border",
                                  tab === id ? "border-primary/40 bg-primary/10 text-primary" : "border-border text-muted-foreground hover:text-foreground")}>
              <Icon className="w-3.5 h-3.5" /> {label}
            </button>
          ))}
        </div>

        <div className="p-5 space-y-3 text-sm">
          {tab === "subscribe" ? (
            <>
              <p className="text-xs text-muted-foreground">
                {t("calendar.import.subscribeIntro")}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {(Object.keys(SOURCES) as SourceId[]).map(id => (
                  <button key={id} onClick={() => { setSource(id); if (name === SOURCES[source].label || !name) setName(SOURCES[id].label); }}
                          className={cn("rounded-full px-3 py-1 text-xs border", source === id ? "border-primary/50 bg-primary/10 text-primary" : "border-border text-muted-foreground")}>
                    {SOURCES[id].label}
                  </button>
                ))}
              </div>
              <ol className="text-xs text-muted-foreground list-decimal pl-4 space-y-0.5">
                {Array.from({ length: SOURCES[source].steps }, (_, i) => <li key={i}>{t(`calendar.import.source.${source}.step${i + 1}`)}</li>)}
                <li>{t("calendar.import.pasteHere")}</li>
              </ol>
              {SOURCES[source].url && (
                <a href={SOURCES[source].url} target="_blank" rel="noopener noreferrer"
                   className="flex items-center justify-center gap-1.5 h-9 rounded-md border border-border text-xs font-medium hover:bg-muted">
                  {t(`calendar.import.source.${source}.open`)} <ExternalLink className="w-3.5 h-3.5" />
                </a>
              )}
              <input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://calendar.google.com/calendar/ical/…/basic.ics"
                     className="w-full h-9 rounded-md border border-border bg-background px-3 text-xs" />
              <div className="flex gap-2">
                <input value={name} onChange={e => setName(e.target.value)} placeholder={t("calendar.import.namePlaceholder")}
                       className="flex-1 h-9 rounded-md border border-border bg-background px-3 text-xs" />
                <button onClick={subscribe} disabled={!url.trim() || !!busy}
                        className="inline-flex items-center gap-1.5 rounded-md bg-primary text-primary-foreground px-3 h-9 text-xs font-medium disabled:opacity-60">
                  {busy === "subscribe" && <Loader2 className="w-3.5 h-3.5 animate-spin" />} {t("calendar.import.subscribe")}
                </button>
              </div>
              {feeds.length > 0 && (
                <div className="pt-2 border-t border-border space-y-1.5">
                  <div className="text-2xs text-muted-foreground font-semibold">{t("calendar.import.subscribed")}</div>
                  {feeds.map(f => (
                    <div key={f.id} className="flex items-center gap-2 text-xs">
                      <span className={cn("w-2 h-2 rounded-full shrink-0", f.last_status === "ok" ? "bg-emerald-500" : "bg-amber-500")} />
                      <span className="flex-1 min-w-0 truncate">
                        <b>{nameOf(f.calendar_id)}</b> · {t("calendar.import.eventCount", { count: f.event_count })} · {f.url_host}
                        {f.last_status !== "ok" && f.last_error ? <span className="text-amber-600"> · {f.last_error}</span> : null}
                      </span>
                      <button onClick={() => syncNow(f)} disabled={!!busy} title={t("calendar.import.syncNow")} className="text-muted-foreground hover:text-foreground">
                        {busy === `sync-${f.id}` ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                      </button>
                      <button onClick={() => unsubscribe(f)} disabled={!!busy} title={t("calendar.import.unsubscribe")} className="text-muted-foreground hover:text-red-500"><Trash2 className="w-3.5 h-3.5" /></button>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <>
              <p className="text-xs text-muted-foreground">
                {t("calendar.import.fileIntro")}
              </p>
              <input ref={fileRef} type="file" accept=".ics,text/calendar" className="hidden"
                     onChange={e => { setFile(e.target.files?.[0] || null); setPreview(null); setDone(null); }} />
              <div className="flex gap-2">
                <button onClick={() => fileRef.current?.click()} className="h-9 rounded-md border border-border px-3 text-xs hover:bg-muted truncate max-w-[55%]">
                  {file ? file.name : t("calendar.import.chooseFile")}
                </button>
                <select value={target ?? ""} onChange={e => setTarget(parseInt(e.target.value, 10))}
                        className="flex-1 h-9 rounded-md border border-border bg-background px-2 text-xs">
                  {mine.map(c => <option key={c.id} value={c.id}>{t("calendar.import.intoCalendar", { name: c.name })}</option>)}
                </select>
              </div>
              {preview && (
                <div className="rounded-md bg-muted/40 p-3 text-xs">
                  <b>{t("calendar.import.eventCount", { count: preview.events })}</b>{preview.series ? t("calendar.import.ofWhichSeries", { count: preview.series }) : ""} · {t("calendar.import.range", { from: day(preview.first), to: day(preview.last) })}
                </div>
              )}
              <div className="flex gap-2 justify-end">
                <button onClick={() => sendFile(true)} disabled={!file || !target || !!busy}
                        className="h-9 rounded-md border border-border px-3 text-xs hover:bg-muted disabled:opacity-60 inline-flex items-center gap-1.5">
                  {busy === "preview" && <Loader2 className="w-3.5 h-3.5 animate-spin" />} {t("calendar.import.preview")}
                </button>
                <button onClick={() => sendFile(false)} disabled={!file || !target || !!busy}
                        className="h-9 rounded-md bg-primary text-primary-foreground px-3 text-xs font-medium disabled:opacity-60 inline-flex items-center gap-1.5">
                  {busy === "import" && <Loader2 className="w-3.5 h-3.5 animate-spin" />} {t("calendar.import.import")}
                </button>
              </div>
            </>
          )}
          {error && <div className="text-xs text-red-500">{error}</div>}
          {done && <div className="text-xs text-emerald-600 dark:text-emerald-400">{done}</div>}
        </div>
      </div>
    </div>
  );
}
