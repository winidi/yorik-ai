/**
 * Add a mail account in three steps, one question per screen:
 *   1. Your address       → Yorik recognises the provider and fills the servers
 *   2. The password       → provider-specific help: which password, with a
 *                           direct link to where you get it (Gmail, iCloud …)
 *   3. How much mail      → then Yorik tests receiving and sending and saves
 * Server names and ports sit under "Advanced" for custom setups; nobody
 * with Gmail or GMX ever sees the word IMAP. POST /api/email/accounts
 * tests both directions before saving; its errors are written for people
 * (email_routes.human_mail_error).
 */

import { useState } from "react";
import { X, AlertCircle, Loader2, ExternalLink, Check, ChevronDown, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { EmailAccount, ProviderPreset } from "./types";
import { IMPORT_SCOPES } from "./types";

interface Props {
  onClose: () => void;
  onSaved: (acct: EmailAccount) => void;
}

// Where each big provider hands out the separate password mail programs
// need. Straight to the page, not a description of how to find it.
const APP_PASSWORD_PAGE: Record<string, { url: string; steps: string[] }> = {
  "Gmail": { url: "https://myaccount.google.com/apppasswords",
    steps: ["Open Google's app passwords page (sign in if asked).", "Type \"Yorik\" as the name and tap Create.", "Copy the 16 letters Google shows and paste them below."] },
  "iCloud Mail": { url: "https://account.apple.com/account/manage",
    steps: ["Open your Apple account page and sign in.", "Go to Sign-In and Security → App-Specific Passwords.", "Create one called \"Yorik\" and paste it below."] },
  "Outlook / Hotmail": { url: "https://account.microsoft.com/security",
    steps: ["Only if you use two-step sign-in: open Microsoft's security page.", "Advanced security options → App passwords → Create.", "Paste it below. Without two-step sign-in, your normal password works."] },
  "Yahoo Mail": { url: "https://login.yahoo.com/account/security",
    steps: ["Open Yahoo's account security page.", "Generate app password, name it \"Yorik\".", "Paste it below."] },
};

type Step = "address" | "password" | "scope";

export function AccountWizard({ onClose, onSaved }: Props) {
  const [step, setStep] = useState<Step>("address");
  const [email, setEmail] = useState("");
  const [preset, setPreset] = useState<ProviderPreset | null>(null);
  const [known, setKnown] = useState(false);
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [isDefault, setIsDefault] = useState(true);
  const [importScope, setImportScope] = useState("days:90");
  const [advanced, setAdvanced] = useState(false);
  const [imap, setImap] = useState({ host: "", port: 993, ssl: true, starttls: false });
  const [smtp, setSmtp] = useState({ host: "", port: 465, ssl: true, starttls: false });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toPassword() {
    setError(null);
    if (!email.includes("@")) { setError("That doesn't look like an email address."); return; }
    setLoading(true);
    try {
      const res = await api.post<{ preset: ProviderPreset }>("/api/email/providers/probe", { email: email.trim() });
      const p = res.preset;
      setPreset(p);
      setKnown(!!p.imap_host);
      setImap({ host: p.imap_host, port: p.imap_port, ssl: p.imap_ssl, starttls: !!p.imap_starttls });
      setSmtp({ host: p.smtp_host, port: p.smtp_port, ssl: p.smtp_ssl, starttls: p.smtp_starttls });
      if (!p.imap_host) setAdvanced(true);   // unknown provider: the servers are needed
    } catch {
      setKnown(false); setAdvanced(true);
    } finally { setLoading(false); setStep("password"); }
  }

  async function submit() {
    setError(null);
    setLoading(true);
    try {
      const acct = await api.post<EmailAccount>("/api/email/accounts", {
        email: email.trim(), display_name: displayName || null, password,
        imap_host: imap.host, imap_port: imap.port, imap_ssl: imap.ssl, imap_starttls: imap.starttls,
        smtp_host: smtp.host, smtp_port: smtp.port, smtp_ssl: smtp.ssl, smtp_starttls: smtp.starttls,
        is_default: isDefault, import_scope: importScope,
      });
      onSaved(acct);
    } catch (e: any) {
      setError(e.message || "Connecting didn't work.");
      setStep("password");
    } finally { setLoading(false); }
  }

  const name = preset?.name || "your provider";
  const help = preset ? APP_PASSWORD_PAGE[preset.name] : undefined;
  const bridge = !!preset?.bridge_required;
  const stepNo = step === "address" ? 1 : step === "password" ? 2 : 3;

  return (
    // z-40 keeps the Dock tappable; no backdrop click, so a mis-tap never
    // throws away a typed password.
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 flex items-center justify-center p-4">
      <div className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-5 pb-3">
          <div>
            <div className="text-xs text-muted-foreground">Step {stepNo} of 3</div>
            <h2 className="font-semibold text-lg">Connect your email</h2>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="px-5 pb-5 space-y-4">
          {error && (
            <div className="flex items-start gap-2 p-3 rounded-xl bg-destructive/10 border border-destructive/30 text-sm">
              <AlertCircle className="w-4 h-4 mt-0.5 shrink-0 text-destructive" />
              <span>{error}</span>
            </div>
          )}

          {step === "address" && (
            <>
              <label className="block">
                <span className="text-sm font-medium">Your email address</span>
                <input type="email" autoFocus value={email} onChange={e => setEmail(e.target.value)}
                       onKeyDown={e => { if (e.key === "Enter") void toPassword(); }}
                       placeholder="you@example.com" autoComplete="email"
                       className="mt-1.5 w-full h-11 px-3 rounded-xl bg-background border border-border focus:outline-none focus:ring-2 focus:ring-ring/40" />
              </label>
              <button onClick={toPassword} disabled={!email || loading}
                      className="w-full h-11 rounded-xl bg-primary text-primary-foreground font-medium disabled:opacity-50 flex items-center justify-center gap-2">
                {loading && <Loader2 className="w-4 h-4 animate-spin" />} Next
              </button>
            </>
          )}

          {step === "password" && (
            <>
              <p className="text-sm">
                {known ? <><Check className="w-4 h-4 inline -mt-0.5 text-emerald-500" /> Yorik knows <b>{name}</b> and has filled in the rest.</>
                       : <>Yorik doesn't know this provider yet. Enter the server details under Advanced; your provider's help pages list them.</>}
              </p>

              {bridge ? (
                <div className="p-3 rounded-xl bg-amber-500/10 border border-amber-500/30 text-sm space-y-2">
                  <p><b>{name}</b> needs its Bridge app on this computer first; then use the password Bridge shows, not your normal one.</p>
                  {preset?.bridge_steps && <ol className="list-decimal pl-5 space-y-1">{preset.bridge_steps.map((s, i) => <li key={i}>{s}</li>)}</ol>}
                  {preset?.docs_url && <a href={preset.docs_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 underline">How to set up Bridge <ExternalLink className="w-3 h-3" /></a>}
                </div>
              ) : help ? (
                <div className="p-3 rounded-xl bg-primary/5 border border-primary/20 text-sm space-y-2">
                  <p><b>{name}</b> wants a separate password for programs like Yorik, not your normal one.</p>
                  <ol className="list-decimal pl-5 space-y-1 text-muted-foreground">{help.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
                  <a href={help.url} target="_blank" rel="noreferrer"
                     className="flex items-center justify-center gap-1.5 h-10 rounded-xl border border-border bg-card font-medium">
                    Get the password from {name} <ExternalLink className="w-3.5 h-3.5" />
                  </a>
                </div>
              ) : preset?.notes ? (
                <div className="p-3 rounded-xl bg-muted/50 text-sm">
                  {preset.notes.replace(/\*\*/g, "")}
                  {preset.docs_url && <> <a href={preset.docs_url} target="_blank" rel="noreferrer" className="underline inline-flex items-center gap-1">Show me how <ExternalLink className="w-3 h-3" /></a></>}
                </div>
              ) : null}

              <label className="block">
                <span className="text-sm font-medium">{bridge ? "Bridge password" : help ? "The password from " + name : "Your email password"}</span>
                <input type="password" autoFocus value={password} onChange={e => setPassword(e.target.value)}
                       autoComplete="current-password"
                       className="mt-1.5 w-full h-11 px-3 rounded-xl bg-background border border-border focus:outline-none focus:ring-2 focus:ring-ring/40" />
              </label>

              <button onClick={() => setAdvanced(a => !a)} className="text-xs text-muted-foreground inline-flex items-center gap-1">
                {advanced ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />} Advanced: server details
              </button>
              {advanced && (
                <div className="space-y-3 p-3 rounded-xl border border-border">
                  <ServerRow label="Incoming mail (IMAP)" v={imap} set={setImap} />
                  <ServerRow label="Outgoing mail (SMTP)" v={smtp} set={setSmtp} starttls />
                </div>
              )}

              <div className="flex gap-2">
                <button onClick={() => { setStep("address"); setError(null); }} className="h-11 px-4 rounded-xl border border-border">Back</button>
                <button onClick={() => { setError(null); setStep("scope"); }} disabled={!password || !imap.host || !smtp.host}
                        className="flex-1 h-11 rounded-xl bg-primary text-primary-foreground font-medium disabled:opacity-50">Next</button>
              </div>
            </>
          )}

          {step === "scope" && (
            <>
              <div>
                <span className="text-sm font-medium">How much mail should Yorik bring in?</span>
                <div className="mt-2 grid gap-2">
                  {IMPORT_SCOPES.map(s => (
                    <button key={s.value} onClick={() => setImportScope(s.value)}
                            className={cn("text-left px-3 py-2.5 rounded-xl border text-sm transition",
                                          importScope === s.value ? "border-primary/60 bg-primary/10" : "border-border hover:bg-muted/50")}>
                      {s.label}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground mt-2">Older mail stays in your mailbox either way; this is only what Yorik reads.</p>
              </div>
              <label className="block">
                <span className="text-sm font-medium">Your name on sent mail <span className="text-muted-foreground font-normal">(optional)</span></span>
                <input value={displayName} onChange={e => setDisplayName(e.target.value)}
                       className="mt-1.5 w-full h-10 px-3 rounded-xl bg-background border border-border" />
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={isDefault} onChange={e => setIsDefault(e.target.checked)} />
                Send from this address by default
              </label>
              <div className="flex gap-2">
                <button onClick={() => setStep("password")} disabled={loading} className="h-11 px-4 rounded-xl border border-border">Back</button>
                <button onClick={submit} disabled={loading}
                        className="flex-1 h-11 rounded-xl bg-primary text-primary-foreground font-medium disabled:opacity-60 flex items-center justify-center gap-2">
                  {loading ? <><Loader2 className="w-4 h-4 animate-spin" /> Testing the connection…</> : "Connect"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function ServerRow({ label, v, set, starttls }: {
  label: string;
  v: { host: string; port: number; ssl: boolean; starttls: boolean };
  set: (x: { host: string; port: number; ssl: boolean; starttls: boolean }) => void;
  starttls?: boolean;
}) {
  return (
    <div className="space-y-1.5">
      <div className="text-xs font-medium">{label}</div>
      <div className="flex gap-2">
        <input value={v.host} onChange={e => set({ ...v, host: e.target.value })} placeholder="Server"
               className="flex-1 min-w-0 h-9 px-2 rounded-md bg-background border border-border text-sm" />
        <input type="number" value={v.port} onChange={e => set({ ...v, port: Number(e.target.value) || 0 })}
               className="w-20 h-9 px-2 rounded-md bg-background border border-border text-sm" />
      </div>
      <div className="flex gap-4 text-xs text-muted-foreground">
        <label className="flex items-center gap-1"><input type="checkbox" checked={v.ssl} onChange={e => set({ ...v, ssl: e.target.checked })} /> SSL</label>
        {starttls && <label className="flex items-center gap-1"><input type="checkbox" checked={v.starttls} onChange={e => set({ ...v, starttls: e.target.checked })} /> STARTTLS</label>}
      </div>
    </div>
  );
}
