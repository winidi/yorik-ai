/**
 * Settings → Apps & accounts → Connections. Where each outside account
 * is connected. The connecting itself happens in the app that uses it
 * (mail in Email, the bank in Finance, …); this list says where, and
 * shows what Yorik can tell about the state from here.
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Mail, CalendarDays, MessageCircle, Landmark, Image, FileText, ChevronRight, Loader2 } from "lucide-react";
import { api } from "@/lib/api";

interface Status {
  email: { configured: boolean; kinds: string[] } | null;
  paperless: { admin_token_set: boolean } | null;
}

export function ConnectionsCard() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [st, setSt] = useState<Status | null>(null);
  const [docsBusy, setDocsBusy] = useState(false);
  const [docsNote, setDocsNote] = useState<string | null>(null);

  useEffect(() => {
    api.get<Status>("/api/system/status").then(setSt).catch(() => setSt(null));
  }, []);

  async function reconnectDocs() {
    setDocsBusy(true); setDocsNote(null);
    try {
      const r = await api.post<{ error?: string; checked?: number }>("/api/documents/sync-paperless");
      setDocsNote(r.error ? r.error : t("settings.connections.docsConnected", { count: r.checked ?? 0 }));
    } catch (e: any) {
      setDocsNote(e?.message || t("settings.connections.failed"));
    } finally { setDocsBusy(false); }
  }

  const rows: { icon: React.ComponentType<{ className?: string }>; name: string; text: string; state?: string; action: string; go: () => void }[] = [
    { icon: Mail, name: t("settings.connections.email.name"), text: t("settings.connections.email.text"),
      state: st?.email ? (st.email.configured ? st.email.kinds[0] || t("settings.connections.connected") : t("settings.connections.noneYet")) : undefined,
      action: t("settings.connections.email.action"), go: () => navigate("/email?add=1") },
    { icon: CalendarDays, name: t("settings.connections.calendar.name"), text: t("settings.connections.calendar.text"),
      action: t("settings.connections.calendar.action"), go: () => navigate("/calendar?import=1") },
    { icon: MessageCircle, name: t("settings.connections.whatsapp.name"), text: t("settings.connections.whatsapp.text"),
      action: t("settings.connections.whatsapp.action"), go: () => navigate("/whatsapp") },
    { icon: Landmark, name: t("settings.connections.bank.name"), text: t("settings.connections.bank.text"),
      action: t("settings.connections.bank.action"), go: () => navigate("/finance") },
    { icon: Image, name: t("settings.connections.photos.name"), text: t("settings.connections.photos.text"),
      action: t("settings.connections.photos.action"), go: () => navigate("/photos") },
  ];

  return (
    <section>
      <h2 className="text-lg font-semibold">{t("settings.connections.title")}</h2>
      <p className="text-sm text-muted-foreground mt-1 mb-4">
        {t("settings.connections.intro")}
      </p>
      <div className="space-y-2">
        {rows.map(r => (
          <button
            key={r.name}
            onClick={r.go}
            className="w-full bg-card border border-border rounded-xl p-4 flex items-start gap-3 text-left hover:bg-muted transition"
          >
            <r.icon className="w-4 h-4 mt-0.5 text-muted-foreground shrink-0" />
            <span className="flex-1 min-w-0">
              <span className="block text-sm font-medium">
                {r.name}
                {r.state && <span className="ml-2 text-xs font-normal text-muted-foreground">{r.state}</span>}
              </span>
              <span className="block text-xs text-muted-foreground mt-0.5">{r.text}</span>
            </span>
            <span className="text-xs text-primary shrink-0 inline-flex items-center gap-0.5">
              {r.action} <ChevronRight className="w-3.5 h-3.5" />
            </span>
          </button>
        ))}
        <div className="bg-card border border-border rounded-xl p-4 flex items-start gap-3">
          <FileText className="w-4 h-4 mt-0.5 text-muted-foreground shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium">
              {t("settings.connections.documents.name")}
              {st?.paperless && (
                <span className="ml-2 text-xs font-normal text-muted-foreground">
                  {st.paperless.admin_token_set ? t("settings.connections.connected") : t("settings.connections.notConnected")}
                </span>
              )}
            </div>
            <div className="text-xs text-muted-foreground mt-0.5">
              {t("settings.connections.documents.text")}
            </div>
            {docsNote && <div className="text-xs mt-1">{docsNote}</div>}
          </div>
          <button
            onClick={reconnectDocs}
            disabled={docsBusy}
            className="text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-muted transition shrink-0 inline-flex items-center gap-1.5 disabled:opacity-60"
          >
            {docsBusy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            {t("settings.connections.documents.action")}
          </button>
        </div>
      </div>
    </section>
  );
}
