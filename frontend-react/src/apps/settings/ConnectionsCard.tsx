/**
 * Settings → Apps & accounts → Connections. Where each outside account
 * is connected. The connecting itself happens in the app that uses it
 * (mail in Email, the bank in Finance, …); this list says where, and
 * shows what Yorik can tell about the state from here.
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Mail, CalendarDays, MessageCircle, Landmark, Image, FileText, ChevronRight, Loader2 } from "lucide-react";
import { api } from "@/lib/api";

interface Status {
  email: { configured: boolean; kinds: string[] } | null;
  paperless: { admin_token_set: boolean } | null;
}

export function ConnectionsCard() {
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
      setDocsNote(r.error ? r.error : `Connected. ${r.checked ?? 0} document(s) in the archive.`);
    } catch (e: any) {
      setDocsNote(e?.message || "That didn't work.");
    } finally { setDocsBusy(false); }
  }

  const rows: { icon: React.ComponentType<{ className?: string }>; name: string; text: string; state?: string; action: string; go: () => void }[] = [
    { icon: Mail, name: "Email", text: "Everyone connects their own mailboxes in the Email app (Gmail, GMX, iCloud …).",
      state: st?.email ? (st.email.configured ? st.email.kinds[0] || "connected" : "none yet") : undefined,
      action: "Add a mailbox", go: () => navigate("/email?add=1") },
    { icon: CalendarDays, name: "Calendar", text: "Bring in the dates from Google or an iPhone; they stay in sync.",
      action: "Bring in a calendar", go: () => navigate("/calendar?import=1") },
    { icon: MessageCircle, name: "WhatsApp", text: "Turn WhatsApp on above, then link the phone in the WhatsApp app with a code.",
      action: "Open WhatsApp", go: () => navigate("/whatsapp") },
    { icon: Landmark, name: "Bank", text: "Turn Finance on above, then add the account in the Finance app.",
      action: "Open Finance", go: () => navigate("/finance") },
    { icon: Image, name: "Photos from phones", text: "Each phone backs up its photos with the Immich app; the Photos app shows how.",
      action: "Open Photos", go: () => navigate("/photos") },
  ];

  return (
    <section>
      <h2 className="text-lg font-semibold">Connections</h2>
      <p className="text-sm text-muted-foreground mt-1 mb-4">
        Where Yorik gets your mail, dates, messages and photos from. Each one is connected in the app that uses it.
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
              Documents
              {st?.paperless && (
                <span className="ml-2 text-xs font-normal text-muted-foreground">
                  {st.paperless.admin_token_set ? "connected" : "not connected"}
                </span>
              )}
            </div>
            <div className="text-xs text-muted-foreground mt-0.5">
              The document archive comes with Yorik and is connected by itself. If the Documents app stays empty, reconnect it.
            </div>
            {docsNote && <div className="text-xs mt-1">{docsNote}</div>}
          </div>
          <button
            onClick={reconnectDocs}
            disabled={docsBusy}
            className="text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-muted transition shrink-0 inline-flex items-center gap-1.5 disabled:opacity-60"
          >
            {docsBusy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            Reconnect
          </button>
        </div>
      </div>
    </section>
  );
}
