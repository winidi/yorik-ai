/**
 * Add-account modal — single pane, all fields visible.
 *
 *   - Provider dropdown at top: picks a known vendor preset, hosts/ports
 *     auto-fill. "Auto-detect from email" is the default; if the user
 *     types an email and the domain matches a known provider, the preset
 *     fills on blur. "Custom" leaves the host fields blank for manual
 *     entry.
 *   - Email + password + IMAP + SMTP are always visible — no Advanced
 *     toggle. Hiding the server fields was busywork because they're
 *     mandatory for every account.
 *   - Submit posts /accounts which itself runs an IMAP + SMTP login test
 *     before persisting, so errors surface inline.
 */

import { useEffect, useState } from "react";
import { X, AlertCircle, Loader2, ExternalLink } from "lucide-react";
import { api } from "@/lib/api";
import type { EmailAccount, ProviderPreset } from "./types";

interface Props {
  onClose: () => void;
  onSaved: (acct: EmailAccount) => void;
}

// Server-shaped provider with the canonical domain key attached. Same
// fields as ProviderPreset plus `key`.
type ProviderEntry = ProviderPreset & { key: string };

const AUTO_KEY = "__auto__";
const CUSTOM_KEY = "__custom__";

export function AccountWizard({ onClose, onSaved }: Props) {
  const [providers, setProviders] = useState<ProviderEntry[]>([]);
  const [selectedKey, setSelectedKey] = useState<string>(AUTO_KEY);
  const [preset, setPreset] = useState<ProviderPreset | null>(null);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [isDefault, setIsDefault] = useState(false);

  const [imap, setImap] = useState({ host: "", port: 993, ssl: true, starttls: false });
  const [smtp, setSmtp] = useState({ host: "", port: 465, ssl: true, starttls: false });

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fetch the vendor list once. Failure is non-fatal — manual entry
  // still works, the dropdown just stays empty.
  useEffect(() => {
    api.get<{ providers: ProviderEntry[] }>("/api/email/providers")
      .then(r => setProviders(r.providers))
      .catch(() => {});
  }, []);

  function applyPreset(p: ProviderPreset) {
    setPreset(p);
    setImap({
      host: p.imap_host, port: p.imap_port,
      ssl: p.imap_ssl, starttls: !!p.imap_starttls,
    });
    setSmtp({
      host: p.smtp_host, port: p.smtp_port,
      ssl: p.smtp_ssl, starttls: p.smtp_starttls,
    });
  }

  function onVendorChange(key: string) {
    setSelectedKey(key);
    if (key === AUTO_KEY) {
      // Re-probe with the current email if we have one.
      if (email) probeFromEmail(email);
      else setPreset(null);
      return;
    }
    if (key === CUSTOM_KEY) {
      setPreset(null);
      setImap({ host: "", port: 993, ssl: true, starttls: false });
      setSmtp({ host: "", port: 465, ssl: true, starttls: false });
      return;
    }
    const p = providers.find(v => v.key === key);
    if (p) applyPreset(p);
  }

  async function probeFromEmail(value: string) {
    if (!value.includes("@")) return;
    try {
      const res = await api.post<{ preset: ProviderPreset }>("/api/email/providers/probe", { email: value });
      // If the detected preset matches one in our list, select it in
      // the dropdown so the UI reflects "we know what this is".
      const match = providers.find(v =>
        v.imap_host === res.preset.imap_host && v.smtp_host === res.preset.smtp_host
      );
      if (match) setSelectedKey(match.key);
      applyPreset(res.preset);
    } catch {
      // probe failures land silently; user can still pick from the
      // dropdown or fill the fields manually.
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

  const submitDisabled = !email || !password || !imap.host || !smtp.host || loading;

  return (
    // z-40 keeps the floating Dock (z-50) tappable above the backdrop so
    // users can navigate away if they decide not to set up email right
    // now. No onClick on the backdrop — losing typed credentials to a
    // mis-tap was the #1 complaint with the previous modal.
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 flex items-center justify-center p-4">
      <div className="bg-card border border-border rounded-xl shadow-2xl w-full max-w-md max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-5 border-b border-border">
          <div>
            <h2 className="font-semibold text-base">Add an email account</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              Pick your provider or fill the servers in manually.
            </p>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          {/* Provider dropdown */}
          <Field label="Provider">
            <select
              value={selectedKey}
              onChange={e => onVendorChange(e.target.value)}
              className="w-full h-9 px-2 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
            >
              <option value={AUTO_KEY}>Auto-detect from email</option>
              {providers.map(p => (
                <option key={p.key} value={p.key}>{p.name}</option>
              ))}
              <option value={CUSTOM_KEY}>Custom (manual setup)</option>
            </select>
          </Field>

          {/* Email + password */}
          <Field label="Email address">
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              onBlur={e => { if (selectedKey === AUTO_KEY) probeFromEmail(e.target.value); }}
              placeholder="you@example.com"
              className="w-full h-9 px-3 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
              autoFocus
            />
          </Field>

          {preset?.bridge_required && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/[0.06] p-3 text-xs leading-relaxed">
              <div className="font-semibold text-amber-700 dark:text-amber-400 mb-1.5">
                ⚠ {preset.name} needs a Bridge app first
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

          <Field label={preset?.bridge_required ? "Bridge password (NOT your web password)" : "Password (or app password)"}>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder={preset?.bridge_required ? "Paste the password Bridge gave you" : "Enter your mailbox password"}
              className="w-full h-9 px-3 rounded-md bg-muted text-sm focus:outline-none focus:ring-2 focus:ring-ring/40"
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

          {/* IMAP — always visible */}
          <div className="p-3 bg-muted/40 rounded-md space-y-3 text-sm">
            <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">Incoming (IMAP)</div>
            <div className="grid grid-cols-[1fr_5rem_4rem] gap-2 items-end">
              <Field label="Host">
                <input value={imap.host} onChange={e => setImap({ ...imap, host: e.target.value })}
                  placeholder="imap.example.com"
                  className="w-full h-8 px-2 rounded bg-card border border-border text-sm" />
              </Field>
              <Field label="Port">
                <input type="number" value={imap.port} onChange={e => setImap({ ...imap, port: +e.target.value })}
                  className="w-full h-8 px-2 rounded bg-card border border-border text-sm" />
              </Field>
              <Field label="SSL">
                <input type="checkbox" checked={imap.ssl}
                  onChange={e => setImap({ ...imap, ssl: e.target.checked, starttls: e.target.checked ? false : imap.starttls })}
                  className="h-4 w-4 mt-1" />
              </Field>
            </div>
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <input type="checkbox" checked={imap.starttls}
                onChange={e => setImap({ ...imap, starttls: e.target.checked, ssl: e.target.checked ? false : imap.ssl })}
                className="h-3.5 w-3.5" />
              STARTTLS (port 143 / Proton Bridge port 1143)
            </label>
          </div>

          {/* SMTP — always visible */}
          <div className="p-3 bg-muted/40 rounded-md space-y-3 text-sm">
            <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">Outgoing (SMTP)</div>
            <div className="grid grid-cols-[1fr_5rem_4rem] gap-2 items-end">
              <Field label="Host">
                <input value={smtp.host} onChange={e => setSmtp({ ...smtp, host: e.target.value })}
                  placeholder="smtp.example.com"
                  className="w-full h-8 px-2 rounded bg-card border border-border text-sm" />
              </Field>
              <Field label="Port">
                <input type="number" value={smtp.port} onChange={e => setSmtp({ ...smtp, port: +e.target.value })}
                  className="w-full h-8 px-2 rounded bg-card border border-border text-sm" />
              </Field>
              <Field label="SSL">
                <input type="checkbox" checked={smtp.ssl}
                  onChange={e => setSmtp({ ...smtp, ssl: e.target.checked, starttls: e.target.checked ? false : smtp.starttls })}
                  className="h-4 w-4 mt-1" />
              </Field>
            </div>
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <input type="checkbox" checked={smtp.starttls}
                onChange={e => setSmtp({ ...smtp, starttls: e.target.checked, ssl: e.target.checked ? false : smtp.ssl })}
                className="h-3.5 w-3.5" />
              STARTTLS (port 587 — Outlook / iCloud / IONOS)
            </label>
          </div>

          {/* Display name + default toggle */}
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
          <button
            onClick={submit}
            disabled={submitDisabled}
            className="px-4 h-9 text-sm rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 flex items-center gap-2"
          >
            {loading && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            Connect &amp; save
          </button>
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
