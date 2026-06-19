/**
 * Add-account wizard. Two-step flow:
 *   1. Email + password → POST /providers/probe → preset comes back
 *      with hosts/ports. User can override.
 *   2. Submit → POST /accounts (which itself tests IMAP + SMTP login
 *      BEFORE persisting). Errors land inline in the form.
 */

import { useState } from "react";
import { X, AlertCircle, Loader2, ChevronDown, ChevronUp, ExternalLink } from "lucide-react";
import { api } from "@/lib/api";
import type { EmailAccount, ProviderPreset } from "./types";
import { cn } from "@/lib/utils";

interface Props {
  onClose: () => void;
  onSaved: (acct: EmailAccount) => void;
}

export function AccountWizard({ onClose, onSaved }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [preset, setPreset] = useState<ProviderPreset | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  // Editable advanced fields, hydrated from the preset on probe.
  const [imap, setImap] = useState({ host: "", port: 993, ssl: true, starttls: false });
  const [smtp, setSmtp] = useState({ host: "", port: 465, ssl: true, starttls: false });
  const [displayName, setDisplayName] = useState("");
  const [isDefault, setIsDefault] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [phase, setPhase] = useState<"start" | "probe" | "form">("start");

  async function probeAndPreFill() {
    setError(null);
    setLoading(true);
    try {
      const res = await api.post<{ preset: ProviderPreset }>("/api/email/providers/probe", { email });
      setPreset(res.preset);
      setImap({
        host: res.preset.imap_host, port: res.preset.imap_port,
        ssl: res.preset.imap_ssl, starttls: !!res.preset.imap_starttls,
      });
      setSmtp({
        host: res.preset.smtp_host, port: res.preset.smtp_port,
        ssl: res.preset.smtp_ssl, starttls: res.preset.smtp_starttls,
      });
      setPhase("form");
    } catch (e: any) {
      setError(e.message || "probe failed");
    } finally {
      setLoading(false);
    }
  }

  async function submit() {
    setError(null);
    setLoading(true);
    try {
      const acct = await api.post<EmailAccount>("/api/email/accounts", {
        email,
        display_name: displayName || null,
        password,
        imap_host: imap.host,
        imap_port: imap.port,
        imap_ssl: imap.ssl,
        imap_starttls: imap.starttls,
        smtp_host: smtp.host,
        smtp_port: smtp.port,
        smtp_ssl: smtp.ssl,
        smtp_starttls: smtp.starttls,
        is_default: isDefault,
      });
      onSaved(acct);
    } catch (e: any) {
      setError(e.message || "save failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    // z-40 keeps the floating Dock (z-50) tappable above the backdrop so
    // users can navigate away if they decide not to set up email right
    // now. NO onClick on the backdrop — losing your typed credentials
    // because you tapped slightly outside a form field was the #1
    // complaint with this modal.
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 flex items-center justify-center p-4">
      <div className="bg-card border border-border rounded-xl shadow-2xl w-full max-w-md max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-5 border-b border-border">
          <div>
            <h2 className="font-semibold text-base">Add an email account</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              {phase === "start" ? "Yorik will detect your provider's settings."
                : phase === "form" && preset
                  ? `${preset.name} · settings filled in`
                  : ""}
            </p>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          {/* Email + password — always visible */}
          <div className="space-y-3">
            <Field label="Email address">
              <input
                type="email"
                value={email}
                onChange={e => { setEmail(e.target.value); setPhase("start"); }}
                placeholder="you@gmail.com"
                className="w-full h-9 px-3 rounded-md bg-muted border border-transparent text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
                autoFocus
              />
            </Field>
            {phase === "form" && preset?.bridge_required && (
              <div className="rounded-md border border-amber-500/30 bg-amber-500/[0.06] p-3 text-xs leading-relaxed">
                <div className="font-semibold text-amber-700 dark:text-amber-400 mb-1.5 flex items-center gap-1.5">
                  <span>⚠ {preset.name} needs a Bridge app first</span>
                </div>
                <p className="text-foreground/80 mb-2">
                  {preset.name} doesn't expose IMAP/SMTP directly. You need their
                  local Bridge app installed AND the password it generates — your
                  normal {preset.name} web-login password won't work.
                </p>
                {preset.bridge_steps && preset.bridge_steps.length > 0 && (
                  <ol className="list-decimal pl-4 space-y-1 text-foreground/85">
                    {preset.bridge_steps.map((step, i) => (
                      <li key={i}>{step}</li>
                    ))}
                  </ol>
                )}
                {preset.docs_url && (
                  <p className="mt-2">
                    <a href={preset.docs_url} target="_blank" rel="noreferrer"
                      className="text-primary inline-flex items-center gap-1 hover:underline font-medium">
                      Open Bridge download page <ExternalLink className="w-3 h-3" />
                    </a>
                  </p>
                )}
              </div>
            )}
            {phase === "form" && (
              <Field label={preset?.bridge_required ? "Bridge password (NOT your web password)" : "Password (or app password)"}>
                <input
                  type="password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder={preset?.bridge_required ? "Paste the password Bridge gave you" : "Enter your mailbox password"}
                  className="w-full h-9 px-3 rounded-md bg-muted border border-transparent text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
                  autoFocus
                />
                {!preset?.bridge_required && preset?.notes && (
                  <p className="text-xs text-muted-foreground mt-1.5 leading-snug">
                    {preset.notes.split("**").map((part, i) =>
                      i % 2 === 1 ? <strong key={i} className="text-foreground">{part}</strong> : part
                    )}
                    {preset.docs_url && (
                      <>{" "}<a href={preset.docs_url} target="_blank" rel="noreferrer"
                        className="text-primary inline-flex items-center gap-1 hover:underline">
                        docs <ExternalLink className="w-3 h-3" />
                      </a></>
                    )}
                  </p>
                )}
              </Field>
            )}
          </div>

          {/* Advanced (hosts/ports) — collapsed by default once preset auto-fills */}
          {phase === "form" && (
            <div>
              <button
                type="button"
                onClick={() => setAdvancedOpen(o => !o)}
                className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
              >
                {advancedOpen ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                Advanced (servers, ports)
              </button>
              {advancedOpen && (
                <div className="mt-3 p-3 bg-muted/40 rounded-md space-y-3 text-sm">
                  <div className="grid grid-cols-[1fr_5rem_4rem] gap-2 items-end">
                    <Field label="IMAP host">
                      <input value={imap.host} onChange={e => setImap({...imap, host: e.target.value})}
                        className="w-full h-8 px-2 rounded bg-card border border-border text-sm" />
                    </Field>
                    <Field label="Port">
                      <input type="number" value={imap.port} onChange={e => setImap({...imap, port: +e.target.value})}
                        className="w-full h-8 px-2 rounded bg-card border border-border text-sm" />
                    </Field>
                    <Field label="SSL">
                      <input type="checkbox" checked={imap.ssl}
                        onChange={e => setImap({...imap, ssl: e.target.checked, starttls: e.target.checked ? false : imap.starttls})}
                        className="h-4 w-4 mt-1" />
                    </Field>
                  </div>
                  <label className="flex items-center gap-2 text-xs text-muted-foreground">
                    <input type="checkbox" checked={imap.starttls}
                      onChange={e => setImap({...imap, starttls: e.target.checked, ssl: e.target.checked ? false : imap.ssl})}
                      className="h-3.5 w-3.5" />
                    IMAP STARTTLS (use when host is plaintext-then-upgrade — e.g. Proton Bridge on 1143, generic port 143)
                  </label>
                  <div className="grid grid-cols-[1fr_5rem_4rem] gap-2 items-end">
                    <Field label="SMTP host">
                      <input value={smtp.host} onChange={e => setSmtp({...smtp, host: e.target.value})}
                        className="w-full h-8 px-2 rounded bg-card border border-border text-sm" />
                    </Field>
                    <Field label="Port">
                      <input type="number" value={smtp.port} onChange={e => setSmtp({...smtp, port: +e.target.value})}
                        className="w-full h-8 px-2 rounded bg-card border border-border text-sm" />
                    </Field>
                    <Field label="SSL">
                      <input type="checkbox" checked={smtp.ssl} onChange={e => setSmtp({...smtp, ssl: e.target.checked})}
                        className="h-4 w-4 mt-1" />
                    </Field>
                  </div>
                  <label className="flex items-center gap-2 text-xs text-muted-foreground">
                    <input type="checkbox" checked={smtp.starttls}
                      onChange={e => setSmtp({...smtp, starttls: e.target.checked})}
                      className="h-3.5 w-3.5" />
                    SMTP uses STARTTLS (port 587 / Outlook / iCloud)
                  </label>
                </div>
              )}
            </div>
          )}

          {/* Display name + default toggle */}
          {phase === "form" && (
            <div className="space-y-3">
              <Field label="Display name (optional)">
                <input value={displayName} onChange={e => setDisplayName(e.target.value)}
                  placeholder='e.g. "Personal Gmail" or "Work"'
                  className="w-full h-9 px-3 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40" />
              </Field>
              <label className="flex items-center gap-2 text-xs">
                <input type="checkbox" checked={isDefault}
                  onChange={e => setIsDefault(e.target.checked)} className="h-4 w-4" />
                <span>Use this account as the default for sending</span>
              </label>
            </div>
          )}

          {/* Errors */}
          {error && (
            <div className="flex gap-2 p-3 bg-destructive/10 border border-destructive/30 rounded-md text-sm">
              <AlertCircle className="w-4 h-4 text-destructive shrink-0 mt-0.5" />
              <span className="text-destructive">{error}</span>
            </div>
          )}
        </div>

        <div className="p-5 border-t border-border flex justify-end gap-2">
          <button onClick={onClose}
            className="px-4 h-9 text-sm rounded-md hover:bg-muted">Cancel</button>
          {phase === "start" || phase === "probe" ? (
            <button
              onClick={probeAndPreFill}
              disabled={!email || loading}
              className="px-4 h-9 text-sm rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-2"
            >
              {loading && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              Continue
            </button>
          ) : (
            <button
              onClick={submit}
              disabled={!password || !imap.host || !smtp.host || loading}
              className="px-4 h-9 text-sm rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-2"
            >
              {loading && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              Connect &amp; save
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider block mb-1">
        {label}
      </span>
      {children}
    </label>
  );
}
