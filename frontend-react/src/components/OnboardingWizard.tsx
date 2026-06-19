/**
 * Onboarding wizard — runs once, after first login (or any time the
 * user's `onboarded_at` is null). Collects the basics Yorik needs to
 * feel personalized from the very first action:
 *
 *   1. Region + language — drives currency/date format, locale-specific
 *      template packs, and (if German/Polish) one-click document
 *      numbering preset install.
 *   2. Address — Compose letterhead uses this on every invoice/letter.
 *   3. Business toggle + name + tax ID + IBAN — separates "freelancer
 *      Yorik" from "household Yorik". Hidden if the user picks Personal.
 *   4. (admin only) Storage location for photos + documents.
 *   5. (admin only) Encrypted backup setup.
 *
 * Storage + Backup are install-level decisions, so members/children
 * skip those steps entirely — both endpoints they call are admin-only
 * and would 403 a non-admin's session.
 *
 * Every step is skippable except marking onboarded. The big "Skip for
 * now" link is always visible so we don't trap users — they can come
 * back via Settings → Profile any time.
 */

import { useState } from "react";
import {
  Loader2, Sparkles, MapPin, Building2, ArrowRight, ArrowLeft,
  CheckCircle2, X, Globe, User as UserIcon, ChevronRight,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import type { YorikUser } from "@/lib/api";
import { StoragePicker } from "@/components/StoragePicker";
import { BackupPicker } from "@/components/BackupPicker";

interface Props {
  user: YorikUser;
  isTenant: boolean;
  onComplete: () => void;
  onSkip: () => void;
}

interface Profile {
  first_name: string;
  last_name: string;
  country: string;
  language: string;
  address_street: string;
  address_postcode: string;
  address_city: string;
  phone: string;
  is_business: boolean;
  business_name: string;
  tax_id: string;
  iban: string;
}

const COUNTRIES = [
  { code: "DE", flag: "🇩🇪", label: "Germany",        language: "de" },
  { code: "AT", flag: "🇦🇹", label: "Austria",        language: "de" },
  { code: "CH", flag: "🇨🇭", label: "Switzerland",    language: "de" },
  { code: "US", flag: "🇺🇸", label: "United States",  language: "en" },
  { code: "GB", flag: "🇬🇧", label: "United Kingdom", language: "en" },
  { code: "PL", flag: "🇵🇱", label: "Poland",         language: "pl" },
  { code: "FR", flag: "🇫🇷", label: "France",         language: "fr" },
  { code: "ES", flag: "🇪🇸", label: "Spain",          language: "es" },
  { code: "IT", flag: "🇮🇹", label: "Italy",          language: "it" },
];

// Countries where invoice-numbering presets are most valuable.
const NUMBERING_PRESETS: Record<string, "de" | "us" | "pl"> = {
  DE: "de", AT: "de", CH: "de",
  US: "us", GB: "us",
  PL: "pl",
};

export function OnboardingWizard({ user, isTenant, onComplete, onSkip }: Props) {
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile>({
    // Split the existing single name field if first/last aren't set yet.
    // The migration backfilled these but the local cached user object
    // from /api/auth/me may still be from a pre-bounce response.
    first_name: user.first_name || (user.name || "").split(" ")[0] || "",
    last_name: user.last_name || (user.name || "").split(" ").slice(1).join(" ") || "",
    country: user.country || guessCountryFromBrowser(),
    language: user.language || guessLanguageFromBrowser(),
    address_street: user.address_street || "",
    address_postcode: user.address_postcode || "",
    address_city: user.address_city || "",
    phone: user.phone || "",
    is_business: !!user.business_name,
    business_name: user.business_name || "",
    tax_id: user.tax_id || "",
    iban: user.iban || "",
  });

  function patch<K extends keyof Profile>(k: K, v: Profile[K]) {
    setProfile(p => ({ ...p, [k]: v }));
  }

  async function saveAndContinue(values: Partial<Profile>, options?: { skip?: boolean; installPreset?: boolean }) {
    setBusy(true);
    setErr(null);
    try {
      const payload: any = {};
      for (const k of Object.keys(values)) {
        const v = (values as any)[k];
        if (k === "is_business") continue; // not a column
        payload[k] = v === "" ? null : v;
      }
      if (options?.skip) payload.onboarded_at = new Date().toISOString();
      if (Object.keys(payload).length > 0) {
        await api.patch("/api/profile", payload);
      }
      if (options?.installPreset && profile.country) {
        const presetKey = NUMBERING_PRESETS[profile.country];
        if (presetKey) {
          try {
            await api.post("/api/compose/series/install-preset?role=admin", { preset: presetKey });
          } catch {
            // Numbering preset is a nice-to-have, never block onboarding on it.
          }
        }
      }
      if (options?.skip) {
        await api.post("/api/onboarding/complete");
        onSkip();
      }
    } catch (e: any) {
      setErr(e.message || "Save failed");
      throw e;
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    setBusy(true);
    setErr(null);
    try {
      await api.patch("/api/profile", {
        first_name: profile.first_name || null,
        last_name: profile.last_name || null,
        country: profile.country || null,
        language: profile.language || null,
        address_street: profile.address_street || null,
        address_postcode: profile.address_postcode || null,
        address_city: profile.address_city || null,
        phone: profile.phone || null,
        business_name: profile.is_business ? (profile.business_name || null) : null,
        tax_id: profile.is_business ? (profile.tax_id || null) : null,
        iban: profile.is_business ? (profile.iban || null) : null,
      });
      // If business + country has a preset, set up numbering automatically.
      if (profile.is_business && NUMBERING_PRESETS[profile.country]) {
        try {
          await api.post("/api/compose/series/install-preset?role=admin", {
            preset: NUMBERING_PRESETS[profile.country],
          });
        } catch {}
      }
      await api.post("/api/onboarding/complete");
      onComplete();
    } catch (e: any) {
      setErr(e.message || "Save failed");
    } finally {
      setBusy(false);
    }
  }

  async function skip() {
    setBusy(true);
    try {
      await api.post("/api/onboarding/complete");
      onSkip();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  // Storage + Backup are install-level decisions owned by the HOST
  // operator. Non-admins get 403s on the underlying endpoints. Tenant
  // admins can't see them either — the host filesystem and bundled
  // Paperless/Immich are shared across all tenants, so per-tenant
  // changes would clobber each other.
  const isAdmin = user.role === "admin" || user.role === "platform_admin";
  const showHostSteps = isAdmin && !isTenant;

  const steps = [
    {
      key: "welcome",
      render: () => <WelcomeStep user={user} onNext={() => setStep(1)} onSkip={skip} />,
    },
    {
      key: "region",
      title: "Where are you based?",
      subtitle: "Yorik adapts dates, currency and document numbering to your country.",
      render: () => <RegionStep profile={profile} patch={patch} />,
    },
    {
      key: "address",
      title: profile.is_business ? "Your business address" : "Your address",
      subtitle: "Yorik puts this in the letterhead when you write invoices, quotes or letters.",
      render: () => <AddressStep profile={profile} patch={patch} />,
    },
    {
      key: "business",
      title: "Personal or business?",
      subtitle: "If you'll send invoices to customers, switch to business mode. You can change this any time.",
      render: () => <BusinessStep profile={profile} patch={patch} />,
    },
    ...(showHostSteps ? [
      {
        key: "storage",
        title: "Where should photos + documents live?",
        subtitle: "DBs stay internal; photos + paperless can grow huge. Move them to an external SSD now, or later in Settings → Storage.",
        render: () => <StorageStep />,
      },
      {
        key: "backup",
        title: "Set up backups",
        subtitle: "Encrypted snapshots of everything that matters. Passphrase is required — lose it and the snapshots are unrecoverable, so save it in a password manager NOW.",
        render: () => <BackupStep />,
      },
    ] : []),
  ];

  const current = steps[step];
  const isFirst = step === 0;
  const isLast = step === steps.length - 1;

  return (
    <div className="min-h-screen flex items-center justify-center bg-background text-foreground px-6 py-8 login-bg">
      <div className="w-full max-w-xl">
        {!isFirst && (
          <div className="flex items-center justify-between mb-6">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span>Step {step} of {steps.length - 1}</span>
              <div className="w-32 h-1 rounded-full bg-muted overflow-hidden">
                <div
                  className="h-full bg-gradient-to-r from-violet-500 to-blue-500 transition-all"
                  style={{ width: `${(step / (steps.length - 1)) * 100}%` }}
                />
              </div>
            </div>
            <button
              onClick={skip}
              disabled={busy}
              className="text-xs text-muted-foreground hover:text-foreground transition disabled:opacity-50"
            >
              Skip for now
            </button>
          </div>
        )}

        <div className="bg-card border border-border rounded-2xl shadow-xl overflow-hidden">
          {current.title && (
            <div className="px-7 pt-7 pb-3">
              <div className="text-xl font-semibold">{current.title}</div>
              {current.subtitle && (
                <div className="text-sm text-muted-foreground mt-1">{current.subtitle}</div>
              )}
            </div>
          )}
          <div className="px-7 pb-7">
            {current.render()}
          </div>

          {!isFirst && (
            <div className="border-t border-border px-7 py-4 bg-muted/20 flex items-center justify-between gap-2">
              {err && (
                <div className="text-xs text-red-500 truncate flex-1">{err}</div>
              )}
              <div className="flex items-center gap-2 ml-auto">
                <button
                  onClick={() => setStep(s => Math.max(0, s - 1))}
                  disabled={busy || step === 1}
                  className="px-3 py-1.5 text-xs rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-50 inline-flex items-center gap-1"
                >
                  <ArrowLeft className="w-3.5 h-3.5" /> Back
                </button>
                {isLast ? (
                  <button
                    onClick={finish}
                    disabled={busy}
                    className={cn(
                      "px-4 py-1.5 text-xs rounded-md font-medium inline-flex items-center gap-1.5 transition",
                      "bg-gradient-to-r from-violet-500 to-blue-500 hover:from-violet-600 hover:to-blue-600 text-white shadow-sm",
                      busy && "opacity-60 cursor-wait",
                    )}
                  >
                    {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
                    Finish setup
                  </button>
                ) : (
                  <button
                    onClick={() => setStep(s => s + 1)}
                    disabled={busy}
                    className={cn(
                      "px-4 py-1.5 text-xs rounded-md font-medium inline-flex items-center gap-1.5 transition",
                      "bg-gradient-to-r from-violet-500 to-blue-500 hover:from-violet-600 hover:to-blue-600 text-white shadow-sm",
                    )}
                  >
                    Continue <ArrowRight className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
      <style>{`
        .login-bg {
          background-image:
            radial-gradient(circle at 30% 15%, hsl(263 70% 60% / 0.10), transparent 50%),
            radial-gradient(circle at 70% 85%, hsl(200 60% 60% / 0.08), transparent 50%);
        }
      `}</style>
    </div>
  );
}

// ─── Step bodies ──────────────────────────────────────────────────────

function WelcomeStep({ user, onNext, onSkip }:
  { user: YorikUser; onNext: () => void; onSkip: () => void }) {
  return (
    <div className="text-center py-6">
      <div className="w-16 h-16 mx-auto rounded-2xl bg-gradient-to-br from-violet-500/30 to-blue-500/30 flex items-center justify-center mb-4 shadow-lg">
        <Sparkles className="w-7 h-7 text-violet-500" />
      </div>
      <div className="text-2xl font-semibold">Hi {user.name.split(" ")[0]}, welcome to Yorik</div>
      <div className="text-sm text-muted-foreground mt-2 max-w-md mx-auto leading-relaxed">
        A 2-minute setup so Yorik knows who you are. We'll personalize letters,
        invoices, and chat replies — and you can skip anything you don't want
        to share right now.
      </div>
      <div className="grid grid-cols-3 gap-3 max-w-md mx-auto mt-7 text-left">
        <FeatureChip icon={Globe}     label="Region & language" />
        <FeatureChip icon={MapPin}    label="Your address" />
        <FeatureChip icon={Building2} label="Personal or business" />
      </div>
      <div className="flex items-center justify-center gap-3 mt-7">
        <button
          onClick={onSkip}
          className="text-xs text-muted-foreground hover:text-foreground transition"
        >
          Skip for now
        </button>
        <button
          onClick={onNext}
          className="px-5 py-2 rounded-md font-medium text-sm inline-flex items-center gap-2 bg-gradient-to-r from-violet-500 to-blue-500 hover:from-violet-600 hover:to-blue-600 text-white shadow-md transition"
        >
          Let's go <ArrowRight className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  );
}

function FeatureChip({ icon: Icon, label }: { icon: React.ComponentType<{ className?: string }>; label: string }) {
  return (
    <div className="bg-muted/40 rounded-lg px-3 py-2 flex items-center gap-2 text-xs">
      <Icon className="w-3.5 h-3.5 text-violet-500" />
      <span className="text-foreground/80">{label}</span>
    </div>
  );
}

function RegionStep({ profile, patch }:
  { profile: Profile; patch: <K extends keyof Profile>(k: K, v: Profile[K]) => void }) {
  return (
    <div className="space-y-4">
      <Field label="Country">
        <div className="grid grid-cols-3 gap-2">
          {COUNTRIES.map(c => (
            <button
              key={c.code}
              onClick={() => {
                patch("country", c.code);
                patch("language", c.language);
              }}
              className={cn(
                "text-left p-2.5 rounded-lg border transition",
                profile.country === c.code
                  ? "bg-violet-500/10 border-violet-500/40"
                  : "bg-muted/40 border-border hover:bg-muted/70",
              )}
            >
              <div className="text-lg">{c.flag}</div>
              <div className="text-xs font-medium mt-0.5">{c.label}</div>
            </button>
          ))}
        </div>
      </Field>
      <Field label="Language Yorik replies in">
        <select
          value={profile.language}
          onChange={e => patch("language", e.target.value)}
          className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
        >
          <option value="en">English</option>
          <option value="de">Deutsch</option>
          <option value="fr">Français</option>
          <option value="es">Español</option>
          <option value="it">Italiano</option>
          <option value="pl">Polski</option>
        </select>
      </Field>
      {NUMBERING_PRESETS[profile.country] && (
        <div className="text-[11px] text-muted-foreground bg-emerald-500/5 border border-emerald-500/20 rounded-md px-3 py-2 leading-relaxed">
          <strong className="text-emerald-600">Bonus:</strong> if you finish setup as a business,
          Yorik will set up legally-compliant invoice numbering for your country
          automatically — you can change everything later.
        </div>
      )}
    </div>
  );
}

function AddressStep({ profile, patch }:
  { profile: Profile; patch: <K extends keyof Profile>(k: K, v: Profile[K]) => void }) {
  return (
    <div className="space-y-3">
      {/* First + last separately — used by Compose for the letterhead
          ("Mit freundlichen Grüßen, <Vorname Nachname>") and by templates
          that need each part on its own line. Stored on the user_profile
          so the LLM never has to ask. */}
      <div className="grid grid-cols-2 gap-3">
        <Field label="First name">
          <input
            autoFocus
            value={profile.first_name}
            onChange={e => patch("first_name", e.target.value)}
            placeholder="Anna"
            className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
          />
        </Field>
        <Field label="Last name">
          <input
            value={profile.last_name}
            onChange={e => patch("last_name", e.target.value)}
            placeholder="Schmidt"
            className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
          />
        </Field>
      </div>
      <Field label="Street and number">
        <input
          value={profile.address_street}
          onChange={e => patch("address_street", e.target.value)}
          placeholder="123 Main Street"
          className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
        />
      </Field>
      <div className="grid grid-cols-3 gap-3">
        <Field label="Postal code">
          <input
            value={profile.address_postcode}
            onChange={e => patch("address_postcode", e.target.value)}
            placeholder="10115"
            className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
          />
        </Field>
        <div className="col-span-2">
          <Field label="City">
            <input
              value={profile.address_city}
              onChange={e => patch("address_city", e.target.value)}
              placeholder="Berlin"
              className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
            />
          </Field>
        </div>
      </div>
      <Field label="Phone (optional)">
        <input
          type="tel"
          value={profile.phone}
          onChange={e => patch("phone", e.target.value)}
          placeholder="+49 30 12345678"
          className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
        />
      </Field>
      <div className="text-[11px] text-muted-foreground">
        Stays on this machine. Yorik only uses it for documents you create.
      </div>
    </div>
  );
}

function BusinessStep({ profile, patch }:
  { profile: Profile; patch: <K extends keyof Profile>(k: K, v: Profile[K]) => void }) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3">
        <button
          onClick={() => patch("is_business", false)}
          className={cn(
            "text-left p-4 rounded-lg border transition",
            !profile.is_business
              ? "bg-violet-500/10 border-violet-500/40"
              : "bg-muted/40 border-border hover:bg-muted/70",
          )}
        >
          <div className="flex items-center gap-2 mb-1">
            <UserIcon className="w-4 h-4 text-violet-500" />
            <span className="font-medium">Personal</span>
          </div>
          <div className="text-[11px] text-muted-foreground leading-relaxed">
            Letters, government forms, family stuff. No invoice numbering needed.
          </div>
        </button>
        <button
          onClick={() => patch("is_business", true)}
          className={cn(
            "text-left p-4 rounded-lg border transition",
            profile.is_business
              ? "bg-violet-500/10 border-violet-500/40"
              : "bg-muted/40 border-border hover:bg-muted/70",
          )}
        >
          <div className="flex items-center gap-2 mb-1">
            <Building2 className="w-4 h-4 text-violet-500" />
            <span className="font-medium">Business</span>
          </div>
          <div className="text-[11px] text-muted-foreground leading-relaxed">
            Send invoices and quotes. Yorik sets up sequential numbering, ZUGFeRD when needed.
          </div>
        </button>
      </div>

      {profile.is_business && (
        <div className="space-y-3 pt-2 border-t border-border">
          <Field label="Business name">
            <input
              value={profile.business_name}
              onChange={e => patch("business_name", e.target.value)}
              placeholder="Sparkle Cleaning Co."
              className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
            />
          </Field>
          <Field label="Tax ID / USt-IdNr / EIN">
            <input
              value={profile.tax_id}
              onChange={e => patch("tax_id", e.target.value)}
              placeholder="DE123456789"
              className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
            />
          </Field>
          <Field label="IBAN (for invoice footer)">
            <input
              value={profile.iban}
              onChange={e => patch("iban", e.target.value)}
              placeholder="DE89 3704 0044 0532 0130 00"
              className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition font-mono"
            />
          </Field>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-[11px] text-muted-foreground mb-1">{label}</div>
      {children}
    </label>
  );
}

// ─── Helpers ──────────────────────────────────────────────────────────

function guessCountryFromBrowser(): string {
  if (typeof navigator === "undefined") return "DE";
  const locale = navigator.language || "";
  const region = locale.split("-")[1]?.toUpperCase();
  if (region && COUNTRIES.some(c => c.code === region)) return region;
  // Fallback per language
  const lang = (locale.split("-")[0] || "").toLowerCase();
  if (lang === "de") return "DE";
  if (lang === "pl") return "PL";
  if (lang === "fr") return "FR";
  if (lang === "es") return "ES";
  if (lang === "it") return "IT";
  if (lang === "en") return "US";
  return "DE";
}

function guessLanguageFromBrowser(): string {
  if (typeof navigator === "undefined") return "en";
  const lang = (navigator.language || "en").split("-")[0].toLowerCase();
  if (["en", "de", "fr", "es", "it", "pl"].includes(lang)) return lang;
  return "en";
}

function StorageStep() {
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground leading-relaxed">
        Yorik's databases (calendar, contacts, chat history) are small and stay on the internal disk.
        Photos and document scans can grow into hundreds of GB — relocate them to an external SSD
        if you have one. <strong>You can skip this step</strong> and decide later in Settings → Storage.
      </p>
      <StoragePicker compact />
    </div>
  );
}

function BackupStep() {
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground leading-relaxed">
        Set a passphrase + a target now and Yorik will auto-snapshot every night. You can change
        any of this later in Settings → Backup, but a Yorik without backups is a Yorik one disk
        away from losing your family's data.
      </p>
      <BackupPicker compact />
    </div>
  );
}
