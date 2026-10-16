/**
 * Settings → System → Phones: how family phones reach Yorik (Tailscale),
 * whether the public join page is on, and the Tailscale access Yorik
 * uses to share this machine with each person it invites.
 * Yorik never switches Serve/Funnel itself; this card shows the one
 * command to run. Backend: /api/invites/setup (member_invites.py).
 */
import { useEffect, useState } from "react";
import { Check, Copy, ExternalLink, Loader2, RefreshCw } from "lucide-react";
import QRCode from "qrcode";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Setup {
  tailscale_running: boolean; dns_name: string | null; base_url: string | null;
  join_page_url: string | null; join_page_port: number; api_configured: boolean; join_page_dir: string;
  runtime?: string; login?: { state: string; auth_url: string | null };
}

// Sign this Yorik in to Tailscale: the link tailscaled is waiting on,
// as a QR code to scan with the phone that has the Tailscale account.
function SignInCode({ url }: { url: string }) {
  const { t } = useTranslation();
  const [img, setImg] = useState<string | null>(null);
  useEffect(() => { QRCode.toDataURL(url, { margin: 1, width: 320 }).then(setImg).catch(() => setImg(null)); }, [url]);
  return (
    <div className="flex flex-col sm:flex-row gap-3 items-start">
      {img && <img src={img} alt={t("settings.phones.signInCodeAlt")} className="w-36 h-36 rounded-lg bg-white p-1" />}
      <div className="space-y-2 text-xs text-muted-foreground">
        <p>{t("settings.phones.signInHint")}</p>
        <a href={url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 underline">{t("settings.phones.openSignIn")} <ExternalLink className="w-3 h-3" /></a>
      </div>
    </div>
  );
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
  const { t } = useTranslation();
  const [done, setDone] = useState(false);
  return (
    <div className="flex items-start gap-2">
      <code className="flex-1 text-xs bg-muted/50 border border-border rounded-md px-2 py-1.5 break-all">{text}</code>
      <button onClick={async () => { try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); } catch { /* select by hand */ } }}
              className="w-8 h-8 rounded-md hover:bg-muted flex items-center justify-center shrink-0" aria-label={t("settings.phones.copy")}>
        {done ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
      </button>
    </div>
  );
}

export function PhonesAccessCard() {
  const { t } = useTranslation();
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
  // While Tailscale waits for its sign-in, check every few seconds so the
  // card turns green by itself once it's done.
  useEffect(() => {
    if (!s || s.tailscale_running || !s.login?.auth_url) return;
    const t = setInterval(() => load(true), 5000);
    return () => clearInterval(t);
  }, [s?.tailscale_running, s?.login?.auth_url]);

  async function save() {
    setBusy(true); setNote(null);
    try {
      const r = await api.put<{ ok: boolean; message: string }>("/api/invites/tailscale-access",
        secret.startsWith("tskey-api-") ? { api_key: secret } : { client_id: clientId, client_secret: secret });
      setNote(r); setSecret(""); load(true);
    } catch (e: any) { setNote({ ok: false, message: e?.message || t("settings.phones.saveFailed") }); }
    finally { setBusy(false); }
  }

  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-muted-foreground">{t("settings.phones.title")}</h2>
        <button onClick={() => load(true)} className="text-muted-foreground hover:text-foreground" title={t("settings.phones.checkAgain")}><RefreshCw className="w-3.5 h-3.5" /></button>
      </div>
      {!s ? <div className="text-sm text-muted-foreground"><Loader2 className="w-4 h-4 animate-spin inline" /></div> : (
        <div className="bg-card border border-border rounded-xl px-4">
          <Row ok={s.tailscale_running} title={s.tailscale_running ? t("settings.phones.tsRunning") : s.login?.auth_url ? t("settings.phones.tsSignIn") : t("settings.phones.tsNotRunning")}>
            {!s.tailscale_running && s.login?.auth_url && <SignInCode url={s.login.auth_url} />}
            {s.base_url
              ? <p className="text-xs text-muted-foreground">{t("settings.phones.reachBefore")}<b>{s.base_url}</b>{t("settings.phones.reachAfter")}</p>
              : <p className="text-xs text-muted-foreground">{t("settings.phones.installBefore")}<code>tailscale serve</code>{t("settings.phones.installAfter")}</p>}
          </Row>
          <Row ok={!!s.join_page_url} title={s.join_page_url ? t("settings.phones.joinOn") : t("settings.phones.joinOff")}>
            {s.join_page_url ? (
              <p className="text-xs text-muted-foreground">{t("settings.phones.joinBefore")}<b>{s.join_page_url}</b>{t("settings.phones.joinAfter")}</p>
            ) : (
              <>
                <p className="text-xs text-muted-foreground">{t("settings.phones.joinExplain")} {s.runtime === "docker" ? t("settings.phones.joinDocker") : t("settings.phones.joinHost")}</p>
                <CopyLine text={s.runtime === "docker"
                  ? "YORIK_TS_SERVE=serve-funnel.json   (then: docker compose up -d tailscale)"
                  : `sudo tailscale funnel --bg --https=${s.join_page_port} ${s.join_page_dir}`} />
                <a href="https://login.tailscale.com/admin/acls" target="_blank" rel="noopener noreferrer" className="text-xs inline-flex items-center gap-1 underline text-muted-foreground">{t("settings.phones.accessControls")} <ExternalLink className="w-3 h-3" /></a>
              </>
            )}
          </Row>
          <Row ok={s.api_configured} title={s.api_configured ? t("settings.phones.shareOk") : t("settings.phones.shareNotYet")}>
            <p className="text-xs text-muted-foreground">
              {t("settings.phones.shareBefore")}<b>{t("settings.phones.shareOnly")}</b>{t("settings.phones.shareAfter")}
            </p>
            <a href="https://login.tailscale.com/admin/settings/oauth" target="_blank" rel="noopener noreferrer" className="text-xs inline-flex items-center gap-1 underline text-muted-foreground">{t("settings.phones.oauthClients")} <ExternalLink className="w-3 h-3" /></a>
            <div className="grid gap-2 pt-1">
              <input value={clientId} onChange={e => setClientId(e.target.value)} placeholder={t("settings.phones.clientId")}
                     className="h-9 px-3 rounded-md bg-background border border-border text-sm" />
              <input value={secret} onChange={e => setSecret(e.target.value)} placeholder={t("settings.phones.clientSecret")} type="password"
                     className="h-9 px-3 rounded-md bg-background border border-border text-sm" />
              <button onClick={save} disabled={busy || !secret}
                      className="h-9 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50">
                {busy ? t("settings.phones.checking") : t("settings.phones.saveAndTest")}
              </button>
              {note && <p className={cn("text-xs", note.ok ? "text-emerald-600 dark:text-emerald-400" : "text-destructive")}>{note.message}</p>}
            </div>
          </Row>
        </div>
      )}
    </section>
  );
}
