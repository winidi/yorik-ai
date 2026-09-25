/**
 * Settings → System → Phones: how family phones reach Yorik (Tailscale),
 * whether the public join page is on, and the Tailscale access Yorik
 * uses to share this machine with each person it invites.
 * Yorik never switches Serve/Funnel itself; this card shows the one
 * command to run. Backend: /api/invites/setup (member_invites.py).
 */
import { useEffect, useState } from "react";
import { Check, Copy, ExternalLink, Loader2, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Setup {
  tailscale_running: boolean; dns_name: string | null; base_url: string | null;
  join_page_url: string | null; join_page_port: number; api_configured: boolean; join_page_dir: string;
}

function Row({ ok, title, children }: { ok: boolean; title: string; children?: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3 py-3 border-t border-border first:border-t-0">
      <span className={cn("w-2.5 h-2.5 rounded-full mt-1.5 shrink-0", ok ? "bg-emerald-500" : "bg-amber-500")} />
      <div className="flex-1 min-w-0 space-y-1.5">
        <div className="text-sm font-medium">{title}</div>
        {children}
      </div>
    </div>
  );
}

function CopyLine({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <div className="flex items-start gap-2">
      <code className="flex-1 text-xs bg-muted/50 border border-border rounded-md px-2 py-1.5 break-all">{text}</code>
      <button onClick={async () => { try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); } catch { /* select by hand */ } }}
              className="w-8 h-8 rounded-md hover:bg-muted flex items-center justify-center shrink-0" aria-label="Copy">
        {done ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
      </button>
    </div>
  );
}

export function PhonesAccessCard() {
  const [s, setS] = useState<Setup | null>(null);
  const [busy, setBusy] = useState(false);
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [note, setNote] = useState<{ ok: boolean; message: string } | null>(null);

  const load = (refresh = false) => {
    (refresh ? api.post<Setup>("/api/invites/setup/refresh") : api.get<Setup>("/api/invites/setup"))
      .then(setS).catch(() => setS(null));
  };
  useEffect(() => { load(); }, []);

  async function save() {
    setBusy(true); setNote(null);
    try {
      const r = await api.put<{ ok: boolean; message: string }>("/api/invites/tailscale-access",
        secret.startsWith("tskey-api-") ? { api_key: secret } : { client_id: clientId, client_secret: secret });
      setNote(r); setSecret(""); load(true);
    } catch (e: any) { setNote({ ok: false, message: e?.message || "Couldn't save." }); }
    finally { setBusy(false); }
  }

  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-muted-foreground">Phones</h2>
        <button onClick={() => load(true)} className="text-muted-foreground hover:text-foreground" title="Check again"><RefreshCw className="w-3.5 h-3.5" /></button>
      </div>
      {!s ? <div className="text-sm text-muted-foreground"><Loader2 className="w-4 h-4 animate-spin inline" /></div> : (
        <div className="bg-card border border-border rounded-xl px-4">
          <Row ok={s.tailscale_running} title={s.tailscale_running ? "Tailscale is running on this machine" : "Tailscale isn't running on this machine"}>
            {s.base_url
              ? <p className="text-xs text-muted-foreground">Family phones reach Yorik at <b>{s.base_url}</b>. Invites point there, never at localhost.</p>
              : <p className="text-xs text-muted-foreground">Install and sign in to Tailscale on this machine, then set up HTTPS with <code>tailscale serve</code> (see Help → Tailscale).</p>}
          </Row>
          <Row ok={!!s.join_page_url} title={s.join_page_url ? "Public join page is on" : "Public join page is off"}>
            {s.join_page_url ? (
              <p className="text-xs text-muted-foreground">Invites open <b>{s.join_page_url}</b> first. It explains Tailscale to people who don't have it yet and holds no data.</p>
            ) : (
              <>
                <p className="text-xs text-muted-foreground">Without it, an invite only works on a phone that already has Tailscale on. To switch it on, allow Funnel for this machine in the Tailscale admin (Access controls), then run once on this machine:</p>
                <CopyLine text={`sudo tailscale funnel --bg --https=${s.join_page_port} ${s.join_page_dir}`} />
                <a href="https://login.tailscale.com/admin/acls" target="_blank" rel="noopener noreferrer" className="text-xs inline-flex items-center gap-1 underline text-muted-foreground">Tailscale access controls <ExternalLink className="w-3 h-3" /></a>
              </>
            )}
          </Row>
          <Row ok={s.api_configured} title={s.api_configured ? "Yorik can share itself with each new person" : "Yorik can't share itself in Tailscale yet"}>
            <p className="text-xs text-muted-foreground">
              With access to your Tailscale account, every invite carries a link that shares <b>only this machine</b> with the new person. Create an OAuth client with write access to devices, then paste it here.
            </p>
            <a href="https://login.tailscale.com/admin/settings/oauth" target="_blank" rel="noopener noreferrer" className="text-xs inline-flex items-center gap-1 underline text-muted-foreground">Tailscale OAuth clients <ExternalLink className="w-3 h-3" /></a>
            <div className="grid gap-2 pt-1">
              <input value={clientId} onChange={e => setClientId(e.target.value)} placeholder="Client ID"
                     className="h-9 px-3 rounded-md bg-background border border-border text-sm" />
              <input value={secret} onChange={e => setSecret(e.target.value)} placeholder="Client secret (or an API key tskey-api-…)" type="password"
                     className="h-9 px-3 rounded-md bg-background border border-border text-sm" />
              <button onClick={save} disabled={busy || !secret}
                      className="h-9 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50">
                {busy ? "Checking…" : "Save and test"}
              </button>
              {note && <p className={cn("text-xs", note.ok ? "text-emerald-600 dark:text-emerald-400" : "text-destructive")}>{note.message}</p>}
            </div>
          </Row>
        </div>
      )}
    </section>
  );
}
