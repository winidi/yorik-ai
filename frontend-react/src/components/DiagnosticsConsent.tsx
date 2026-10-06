/**
 * Diagnostics consent — the three switches that decide what Yorik may
 * tell its makers (backend/diagnostics), and the one-time step after
 * setup that asks the admin.
 *
 * Nothing is pre-ticked, "nothing" is a valid answer, and the step is
 * its own screen rather than part of finishing setup, so saying yes is
 * never bundled with getting on with the app. The same switches live in
 * Settings → Privacy (DiagnosticsCard) with "reset identity" and a link
 * to what was sent. Plan with Dirk, 2026-10-06.
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronUp, Loader2, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface DiagConsent {
  asked: boolean;
  asked_at?: string | null;
  counts: boolean;
  usage: boolean;
  errors: boolean;
  is_admin: boolean;
  llm_local: boolean;
  collector_configured: boolean;
  install_id: string | null;
}

interface RegistryField {
  path: string; tier: number; type: string; purpose: string; values: string[] | null; max_len: number | null;
}

const TIERS: Array<{ key: "counts" | "usage" | "errors"; tier: number }> = [
  { key: "counts", tier: 1 }, { key: "usage", tier: 2 }, { key: "errors", tier: 3 },
];

function Switch({ on, onChange, disabled, label }: { on: boolean; onChange: (v: boolean) => void; disabled?: boolean; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={() => !disabled && onChange(!on)}
      disabled={disabled}
      className={cn("shrink-0 relative inline-flex h-6 w-11 items-center rounded-full transition",
        on ? "bg-violet-500" : "bg-muted", disabled && "opacity-60 cursor-wait")}
    >
      <span className={cn("inline-block h-4 w-4 transform rounded-full bg-white transition", on ? "translate-x-6" : "translate-x-1")} />
    </button>
  );
}

/** The three switches with their explanations and the field list. */
export function DiagnosticsSwitches({ value, onChange, disabled, readOnly }: {
  value: Pick<DiagConsent, "counts" | "usage" | "errors">;
  onChange: (next: Pick<DiagConsent, "counts" | "usage" | "errors">) => void;
  disabled?: boolean;
  readOnly?: boolean;
}) {
  const { t } = useTranslation();
  const [fields, setFields] = useState<RegistryField[] | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open || fields) return;
    api.get<{ fields: RegistryField[] }>("/api/diagnostics/registry").then(r => setFields(r.fields)).catch(() => setFields([]));
  }, [open, fields]);

  const texts: Record<string, { title: string; what: string; example: string }> = {
    counts: {
      title: t("settings.diagnostics.counts.title", "Counts and versions"),
      what: t("settings.diagnostics.counts.what", "Once a day: Yorik's version, the kind of computer, how many people use it (as a range), how often the chat was used, thumbs up and down. Numbers only, never content."),
      example: 'version 0.3 · linux-x64 · users 3-4 · chat_turns 10-49',
    },
    usage: {
      title: t("settings.diagnostics.usage.title", "Which features are used"),
      what: t("settings.diagnostics.usage.what", "Which kinds of features get used — mail, WhatsApp, documents, calendar, bank — as ranges. Not which mail, not whose."),
      example: 'features: email 50-199, documents 1-9, bank 0',
    },
    errors: {
      title: t("settings.diagnostics.errors.title", "Error reports"),
      what: t("settings.diagnostics.errors.what", "When something goes wrong, Yorik writes a report in which every person, address, phone and chat is replaced by a number (person_7). You see the exact report and send it yourself, or not. Never mail text, documents or messages."),
      example: 'question: "wo ist die rechnung von person_7" · mail from person_7: exists, indexed, search rank 9 · answer: found nothing',
    },
  };

  return (
    <div className="space-y-3">
      {TIERS.map(({ key, tier }) => (
        <div key={key} className="flex items-start justify-between gap-4 rounded-xl border border-border p-4">
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium">{texts[key].title}</div>
            <p className="text-xs text-muted-foreground mt-1">{texts[key].what}</p>
            <div className="mt-2 text-2xs font-mono text-muted-foreground/80 break-words">
              {t("settings.diagnostics.example", "What leaves the house, for example:")} {texts[key].example}
            </div>
          </div>
          {readOnly
            ? <span className={cn("text-xs shrink-0", value[key] ? "text-emerald-600" : "text-muted-foreground")}>{value[key] ? t("common.on", "on") : t("common.off", "off")}</span>
            : <Switch on={value[key]} disabled={disabled} label={texts[key].title} onChange={v => onChange({ ...value, [key]: v })} />}
        </div>
      ))}
      <button type="button" onClick={() => setOpen(o => !o)} className="text-xs text-muted-foreground hover:text-foreground inline-flex items-center gap-1">
        {open ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
        {t("settings.diagnostics.allFields", "Every field Yorik can send")}
      </button>
      {open && (
        <div className="rounded-xl border border-border max-h-64 overflow-y-auto text-xs">
          {fields === null && <div className="p-3 text-muted-foreground"><Loader2 className="w-3.5 h-3.5 animate-spin inline" /></div>}
          {fields && fields.length === 0 && <div className="p-3 text-muted-foreground">{t("settings.diagnostics.noFields", "The list could not be loaded.")}</div>}
          {fields && fields.length > 0 && (
            <table className="w-full">
              <tbody>
                {fields.map(f => (
                  <tr key={f.path} className="border-b border-border/60 last:border-0 align-top">
                    <td className="px-3 py-1.5 font-mono whitespace-nowrap">{f.path}</td>
                    <td className="px-2 py-1.5 text-muted-foreground whitespace-nowrap">{t("settings.diagnostics.tier", "tier")} {f.tier}</td>
                    <td className="px-3 py-1.5 text-muted-foreground">{f.purpose}{f.values ? ` (${f.values.join(", ")})` : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

/** The one screen after setup. Shown to the admin once (user.diag_consent_asked). */
export function DiagnosticsConsentStep({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const [value, setValue] = useState({ counts: false, usage: false, errors: false });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    setBusy(true); setErr(null);
    try {
      await api.put("/api/diagnostics/consent", value);
      onDone();
    } catch (e: any) {
      setErr(e?.message || t("settings.diagnostics.saveFailed", "Could not save. Try again."));
    } finally {
      setBusy(false);
    }
  }

  return (
    // The page is its own scroll container: the body does not scroll
    // (index.css), and on a phone this step is taller than the screen.
    <div className="h-screen overflow-y-auto bg-background text-foreground login-bg">
     <div className="min-h-full flex items-center justify-center px-4 sm:px-6 py-6 sm:py-8">
      <div className="w-full max-w-xl">
        <div className="bg-card border border-border rounded-2xl shadow-xl overflow-hidden">
          <div className="px-5 sm:px-7 pt-5 sm:pt-7 pb-3">
            <div className="flex items-center gap-2 text-xl font-semibold">
              <ShieldCheck className="w-5 h-5 text-violet-500" />
              {t("onboarding.diagnostics.title", "May Yorik tell its makers when something goes wrong?")}
            </div>
            <div className="text-sm text-muted-foreground mt-1">
              {t("onboarding.diagnostics.subtitle", "Everything is off. Each switch is a separate yes, you can change them any time in Settings → Privacy, and Yorik never sends mail, documents or messages — people become numbers before anything leaves this computer.")}
            </div>
          </div>
          <div className="px-5 sm:px-7 pb-5 sm:pb-7">
            <DiagnosticsSwitches value={value} onChange={setValue} disabled={busy} />
          </div>
          <div className="border-t border-border px-5 sm:px-7 py-4 bg-muted/20 flex items-center justify-between gap-2">
            <div className="text-xs text-red-500 truncate flex-1">{err}</div>
            <button
              onClick={save}
              disabled={busy}
              className={cn("px-4 py-1.5 text-xs rounded-md font-medium inline-flex items-center gap-1.5 transition",
                "bg-gradient-to-r from-violet-500 to-blue-500 hover:from-violet-600 hover:to-blue-600 text-white shadow-sm",
                busy && "opacity-60 cursor-wait")}
            >
              {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              {Object.values(value).some(Boolean)
                ? t("onboarding.diagnostics.save", "Save my choices")
                : t("onboarding.diagnostics.saveNone", "Keep everything off")}
            </button>
          </div>
        </div>
      </div>
     </div>
    </div>
  );
}

/** Settings → Privacy: the same switches, plus reset and the link to what was sent. */
export function DiagnosticsCard({ toast, onOpenReports }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
  onOpenReports?: () => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<DiagConsent | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setState(await api.get<DiagConsent>("/api/diagnostics/consent")); } catch { setState(null); }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function change(next: Pick<DiagConsent, "counts" | "usage" | "errors">) {
    if (!state) return;
    setBusy(true);
    try {
      const r = await api.put<DiagConsent>("/api/diagnostics/consent", next);
      setState({ ...state, ...r });
      toast(t("settings.diagnostics.saved", "Saved"), "success");
    } catch (e: any) {
      toast(e?.message || "Failed to save", "error");
    } finally { setBusy(false); }
  }

  async function reset() {
    setBusy(true);
    try {
      const r = await api.post<{ pseudonyms_forgotten: number }>("/api/diagnostics/identity/reset", {});
      toast(t("settings.diagnostics.resetDone", "New identity. {{n}} pseudonyms forgotten.", { n: r.pseudonyms_forgotten }), "success");
      await load();
    } catch (e: any) {
      toast(e?.message || "Failed", "error");
    } finally { setBusy(false); }
  }

  if (!state) return null;
  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <h3 className="text-xs font-semibold text-muted-foreground mb-1">{t("settings.diagnostics.title", "Diagnostics for Yorik's makers")}</h3>
      <p className="text-xs text-muted-foreground mb-3">
        {state.is_admin
          ? t("settings.diagnostics.intro", "What Yorik may report about how it works here. Off means nothing leaves this computer. People, addresses and chats are replaced by numbers before any report; you review every error report before it is sent.")
          : t("settings.diagnostics.introMember", "The admin decides what Yorik may report about how it works here. You review every error report from your own chats before it is sent.")}
        {!state.llm_local && state.errors && (
          <span className="block mt-1 text-amber-600">{t("settings.diagnostics.cloudNote", "Your model runs in a cloud, so free text in reports is left out entirely (only a local model may check names).")}</span>
        )}
      </p>
      <DiagnosticsSwitches value={state} onChange={change} disabled={busy} readOnly={!state.is_admin} />
      <div className="mt-4 flex flex-wrap items-center gap-3 text-xs">
        {onOpenReports && (
          <button onClick={onOpenReports} className="underline text-muted-foreground hover:text-foreground">
            {t("settings.diagnostics.whatWasSent", "What Yorik has sent")}
          </button>
        )}
        {state.is_admin && (
          <button onClick={reset} disabled={busy} className="underline text-muted-foreground hover:text-foreground disabled:opacity-50">
            {t("settings.diagnostics.reset", "Reset identity (forget every pseudonym)")}
          </button>
        )}
        {state.is_admin && state.asked_at && (
          <span className="text-muted-foreground/70">{t("settings.diagnostics.askedAt", "Answered")} {state.asked_at.slice(0, 10)}</span>
        )}
      </div>
    </div>
  );
}
