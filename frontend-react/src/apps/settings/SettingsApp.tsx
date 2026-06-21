/**
 * Settings — the hub. Tabs:
 *   - Profile        : edit your address / business / IBAN / language
 *   - Numbering      : opens the SeriesManager (already lives in Compose)
 *   - Quality        : per-LLM success rates for skills/templates/turns
 *   - Connectors     : deep-link to the vanilla connectors page until we
 *                      finish porting it; better than a dead link.
 *
 * Three-pane shell to match the rest of the React apps.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Loader2, Settings as Cog, User as UserIcon, Hash, BarChart3,
  Plug, ExternalLink, Save, CheckCircle2, AlertCircle, Sparkles,
  RefreshCw, ThumbsUp, ThumbsDown, FileText, Lightbulb, Database,
  Menu, Cpu, Wifi, Search, Sun, Moon, Monitor, Mic, Grid3x3, MonitorSmartphone, Square,
  Users, UsersRound, UserPlus, KeyRound, Trash2, Power, Copy, X,
  HardDrive, ArrowRightLeft, AlertTriangle, Shield, Upload,
  Tag as TagIcon, XCircle, Info, ScrollText, ChevronRight, Puzzle,
  Store, Download, BadgeCheck, Lock, Globe, Package, Plus, Home,
} from "lucide-react";
import { useTriPane, MobileBackdrop, mobileAsideLeft } from "@/components/MobileShell";
import { AppInstallConsentDialog } from "@/components/AppInstallConsentDialog";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useAuth } from "@/components/AuthGate";
import { Dock } from "@/components/Dock";
import { StoragePicker } from "@/components/StoragePicker";
import { BackupPicker } from "@/components/BackupPicker";
import { SeriesManager } from "@/apps/compose/SeriesManager";
import { DevicesTab } from "./DevicesTab";
import { HouseholdsTab } from "./HouseholdsTab";

type Tab = "profile" | "llm" | "users" | "spaces" | "households" | "apps" | "marketplace" | "installed" | "skills" | "numbering" | "quality" | "connectors" | "extensions" | "storage" | "embeddings" | "backup" | "logs" | "devices";

// `adminOnly: true` hides the tab from non-admin users in the sidebar.
// The corresponding backend endpoints already 403 for non-admins, so
// this is purely a UX fix — members were seeing tabs that would show
// errors / blanks when they clicked through. Storage and Embeddings
// are the clearest cases (host-level filesystem + global embedder
// config) where the panel is meaningless to a member.
// hostOnly = tab manages state shared across all tenants (host
// filesystem, host config.env, bundled LLM/embedder, install-wide
// registries). Tenant Yoriks hide these because changes there would
// either clobber other tenants or have no effect inside the tenant's
// own DB. adminOnly = member/child users hide it; orthogonal axis.
const TABS: { id: Tab; label: string; icon: React.ComponentType<{ className?: string }>; color: string; adminOnly?: boolean; hostOnly?: boolean }[] = [
  { id: "profile",    label: "Profile",     icon: UserIcon, color: "text-violet-500 bg-violet-500/10" },
  { id: "devices",    label: "Devices",     icon: MonitorSmartphone, color: "text-blue-500 bg-blue-500/10" },
  { id: "llm",        label: "LLM",         icon: Cpu,      color: "text-blue-500 bg-blue-500/10",     adminOnly: true, hostOnly: true },
  { id: "users",      label: "Users",       icon: Users,    color: "text-cyan-500 bg-cyan-500/10" },
  { id: "households", label: "Households",  icon: Home,     color: "text-orange-500 bg-orange-500/10", adminOnly: true, hostOnly: true },
  { id: "spaces",     label: "Spaces",      icon: Shield,   color: "text-teal-500 bg-teal-500/10" },
  { id: "apps",       label: "Apps",        icon: Grid3x3,  color: "text-fuchsia-500 bg-fuchsia-500/10" },
  { id: "marketplace",label: "Marketplace", icon: Store,    color: "text-pink-500 bg-pink-500/10",     adminOnly: true, hostOnly: true },
  { id: "installed",  label: "Installed",   icon: Package,  color: "text-pink-500 bg-pink-500/10",     adminOnly: true, hostOnly: true },
  { id: "skills",     label: "Skills",      icon: Lightbulb,color: "text-yellow-500 bg-yellow-500/10", adminOnly: true, hostOnly: true },
  { id: "numbering",  label: "Numbering",   icon: Hash,     color: "text-rose-500 bg-rose-500/10" },
  { id: "quality",    label: "Quality",     icon: BarChart3,color: "text-emerald-500 bg-emerald-500/10" },
  { id: "connectors", label: "Connectors",  icon: Plug,     color: "text-amber-500 bg-amber-500/10",   adminOnly: true, hostOnly: true },
  { id: "extensions", label: "Extensions",  icon: Puzzle,   color: "text-indigo-500 bg-indigo-500/10", adminOnly: true, hostOnly: true },
  { id: "storage",    label: "Storage",     icon: HardDrive,color: "text-sky-500 bg-sky-500/10",     adminOnly: true, hostOnly: true },
  { id: "embeddings", label: "Embeddings",  icon: Sparkles, color: "text-violet-500 bg-violet-500/10", adminOnly: true, hostOnly: true },
  { id: "backup",     label: "Backup",      icon: Shield,   color: "text-emerald-500 bg-emerald-500/10", adminOnly: true, hostOnly: true },
  { id: "logs",       label: "Logs",        icon: ScrollText,color: "text-orange-500 bg-orange-500/10" },
];

export function SettingsApp() {
  const auth = useAuth();
  const isAdmin = auth.user.role === "admin" || auth.user.role === "platform_admin";
  // Filter once per render — cheap, and lets the sidebar map + the
  // selected-tab guard read the same list. hostOnly hides tabs whose
  // panels manage host-shared state (LLM, storage, marketplace etc.)
  // when this Yorik is a tenant.
  const visibleTabs = TABS.filter(t =>
    (isAdmin || !t.adminOnly) && (!auth.isTenant || !t.hostOnly)
  );
  const [tab, setTab] = useState<Tab>("profile");
  // If the persisted/current tab is admin-only and the user isn't an
  // admin (e.g., a role downgrade happened, or stale local state),
  // fall back to Profile rather than rendering an empty pane.
  const activeTab: Tab = visibleTabs.some(t => t.id === tab) ? tab : "profile";
  const [toasts, setToasts] = useState<Array<{ id: number; kind: "info" | "success" | "error"; text: string }>>([]);

  // Memoise — every child tab depends on `toast` in its useEffect
  // dependency arrays. Without useCallback this function is a fresh
  // reference on every parent render, which propagates through the
  // children's refresh callbacks, retriggers their useEffects, fires
  // another fetch, sets state, re-renders the parent, and the cycle
  // repeats at network speed. The Logs tab in particular ground out
  // ~2500 self-calls to /api/system/errors in a few minutes and
  // tripped the per-IP rate limit, surfacing as "Could not load
  // logs: rate limit exceeded" to the user. Stable identity is the
  // fix — setToasts updaters are already shape-stable (they take a
  // function, not a snapshot), so the empty dep array is safe.
  const toast = useCallback((text: string, kind: "info" | "success" | "error" = "info") => {
    const id = Date.now() + Math.random();
    setToasts(t => [...t, { id, kind, text }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 4500);
  }, []);

  const tri = useTriPane();

  return (
    <div className="flex h-screen bg-background text-foreground pb-16 relative">
      <MobileBackdrop show={tri.leftOpen} onClick={tri.closeAll} />
      <aside className={cn(
        "w-[260px] border-r border-border flex flex-col bg-sidebar shrink-0",
        mobileAsideLeft(tri.leftOpen),
      )}>
        <header className="h-16 px-5 flex items-center gap-2.5 border-b border-border">
          <div className="w-8 h-8 rounded-full bg-muted/60 flex items-center justify-center">
            <Cog className="w-4 h-4 text-muted-foreground" />
          </div>
          <div>
            <div className="font-semibold leading-none">Settings</div>
            <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5">
              {auth.user.name} · {auth.user.role}
            </div>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {visibleTabs.map(t => (
            <button
              key={t.id}
              onClick={() => { setTab(t.id); tri.closeAll(); }}
              className={cn(
                "w-full text-left px-3 py-2 rounded-lg flex items-center gap-2.5 transition",
                activeTab === t.id
                  ? "bg-sidebar-accent shadow-sm"
                  : "hover:bg-sidebar-accent/50",
              )}
            >
              <div className={cn("w-7 h-7 rounded-md flex items-center justify-center shrink-0", t.color)}>
                <t.icon className="w-3.5 h-3.5" />
              </div>
              <span className="text-sm font-medium">{t.label}</span>
            </button>
          ))}
        </div>

        <footer className="border-t border-border px-4 py-3 text-xs">
          <button
            onClick={() => auth.logout()}
            className="w-full text-left text-muted-foreground hover:text-foreground transition"
          >
            Sign out
          </button>
        </footer>
      </aside>

      <section className="flex-1 overflow-y-auto bg-background min-w-0">
        <div className="md:hidden h-12 px-3 border-b border-border bg-background/85 backdrop-blur flex items-center gap-2 sticky top-0 z-30">
          <button
            onClick={() => tri.setLeftOpen(true)}
            aria-label="Open menu"
            className="w-9 h-9 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center justify-center"
          >
            <Menu className="w-4 h-4" />
          </button>
          <div className="text-sm font-medium truncate flex-1">
            {visibleTabs.find(t => t.id === activeTab)?.label}
          </div>
        </div>
        <div className="max-w-3xl mx-auto px-4 sm:px-8 py-6 sm:py-8">
          {activeTab === "profile"    && <ProfileTab toast={toast} />}
          {activeTab === "devices"    && <DevicesTab toast={toast} />}
          {activeTab === "llm"        && <LlmTab toast={toast} />}
          {activeTab === "users"      && <UsersTab toast={toast} />}
          {activeTab === "households" && <HouseholdsTab toast={toast} />}
          {activeTab === "spaces"     && <SpacesTab toast={toast} />}
          {activeTab === "apps"       && <AppsTab toast={toast} />}
          {activeTab === "marketplace"&& <MarketplaceTab toast={toast} />}
          {activeTab === "installed"  && <InstalledAppsTab toast={toast} />}
          {activeTab === "skills"     && <SkillsTab toast={toast} />}
          {activeTab === "numbering"  && <NumberingTab toast={toast} />}
          {activeTab === "quality"    && <QualityTab toast={toast} />}
          {activeTab === "connectors" && <ConnectorsTab />}
          {activeTab === "extensions" && <ExtensionsTab toast={toast} />}
          {activeTab === "storage"    && <StorageTab toast={toast} />}
          {activeTab === "embeddings" && <EmbeddingsTab toast={toast} />}
          {activeTab === "backup"     && <BackupTab toast={toast} />}
          {activeTab === "logs"       && <LogsTab toast={toast} />}
        </div>
      </section>

      {/* Toasts */}
      <div className="fixed bottom-20 right-6 z-[1100] flex flex-col gap-2">
        {toasts.map(t => (
          <div
            key={t.id}
            className={cn(
              "px-4 py-2.5 rounded-lg shadow-lg border text-sm font-medium",
              t.kind === "success" && "bg-emerald-500/10 border-emerald-500/30 text-emerald-600",
              t.kind === "error"   && "bg-red-500/10 border-red-500/30 text-red-600",
              t.kind === "info"    && "bg-card border-border text-foreground",
            )}
          >
            <div className="flex items-center gap-2">
              {t.kind === "success" && <CheckCircle2 className="w-4 h-4" />}
              {t.kind === "error"   && <AlertCircle  className="w-4 h-4" />}
              <span>{t.text}</span>
            </div>
          </div>
        ))}
      </div>

      <Dock activeAppId="settings" />
    </div>
  );
}

// ─── Profile tab ──────────────────────────────────────────────────────

function ProfileTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const auth = useAuth();
  const u = auth.user;
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    name:             u.name || "",
    // First + last split — used by Compose for letterheads. Fall back
    // to splitting `name` on the first space when these aren't set
    // yet (pre-migration-017 sessions).
    first_name:       u.first_name || (u.name || "").split(" ")[0] || "",
    last_name:        u.last_name  || (u.name || "").split(" ").slice(1).join(" ") || "",
    language:         u.language || "en",
    country:          u.country || "",
    address_street:   u.address_street || "",
    address_postcode: u.address_postcode || "",
    address_city:     u.address_city || "",
    phone:            u.phone || "",
    business_name:    u.business_name || "",
    tax_id:           u.tax_id || "",
    iban:             u.iban || "",
  });

  function set<K extends keyof typeof form>(k: K, v: typeof form[K]) {
    setForm(p => ({ ...p, [k]: v }));
  }

  async function save() {
    setBusy(true);
    try {
      const payload: any = {};
      for (const k of Object.keys(form)) {
        const v = (form as any)[k];
        payload[k] = v === "" ? null : v;
      }
      await api.patch("/api/profile", payload);
      await auth.refresh();
      toast("Profile saved", "success");
    } catch (e: any) {
      toast(`Save failed: ${e.message}`, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Profile</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Your address and business info. Used as the sender block on every document
          Compose generates.
        </p>
      </header>

      <div className="space-y-5">
        <Card title="You">
          <div className="grid grid-cols-2 gap-3">
            <Field label="First name">
              <input value={form.first_name} onChange={e => set("first_name", e.target.value)} className={inputClass} />
            </Field>
            <Field label="Last name">
              <input value={form.last_name} onChange={e => set("last_name", e.target.value)} className={inputClass} />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-3 mt-3">
            <Field label="Display name (login + autocomplete)">
              <input value={form.name} onChange={e => set("name", e.target.value)} className={inputClass} />
            </Field>
            <Field label="Email">
              <input value={u.email} disabled className={cn(inputClass, "opacity-60 cursor-not-allowed")} />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-3 mt-3">
            <Field label="Language">
              <select value={form.language} onChange={e => set("language", e.target.value)} className={inputClass}>
                <option value="en">English</option>
                <option value="de">Deutsch</option>
                <option value="fr">Français</option>
                <option value="es">Español</option>
                <option value="it">Italiano</option>
                <option value="pl">Polski</option>
              </select>
            </Field>
            <Field label="Country">
              <select value={form.country} onChange={e => set("country", e.target.value)} className={inputClass}>
                <option value="">—</option>
                <option value="DE">🇩🇪 Germany</option>
                <option value="AT">🇦🇹 Austria</option>
                <option value="CH">🇨🇭 Switzerland</option>
                <option value="US">🇺🇸 United States</option>
                <option value="GB">🇬🇧 United Kingdom</option>
                <option value="PL">🇵🇱 Poland</option>
                <option value="FR">🇫🇷 France</option>
                <option value="ES">🇪🇸 Spain</option>
                <option value="IT">🇮🇹 Italy</option>
              </select>
            </Field>
          </div>
        </Card>

        <Card title="Address">
          <Field label="Street and number">
            <input value={form.address_street} onChange={e => set("address_street", e.target.value)} className={inputClass} />
          </Field>
          <div className="grid grid-cols-3 gap-3 mt-3">
            <Field label="Postal code">
              <input value={form.address_postcode} onChange={e => set("address_postcode", e.target.value)} className={inputClass} />
            </Field>
            <div className="col-span-2">
              <Field label="City">
                <input value={form.address_city} onChange={e => set("address_city", e.target.value)} className={inputClass} />
              </Field>
            </div>
          </div>
          <div className="mt-3">
            <Field label="Phone">
              <input value={form.phone} onChange={e => set("phone", e.target.value)} className={inputClass} />
            </Field>
          </div>
        </Card>

        <Card title="Business">
          <Field label="Business name (leave blank for personal use)">
            <input value={form.business_name} onChange={e => set("business_name", e.target.value)} className={inputClass} />
          </Field>
          <div className="grid grid-cols-2 gap-3 mt-3">
            <Field label="Tax ID / USt-IdNr / EIN">
              <input value={form.tax_id} onChange={e => set("tax_id", e.target.value)} className={inputClass} />
            </Field>
            <Field label="IBAN">
              <input value={form.iban} onChange={e => set("iban", e.target.value)} className={cn(inputClass, "font-mono")} />
            </Field>
          </div>
        </Card>

        <SignatureUpload toast={toast} />
        <ThemeToggle />
        <Card title="Voice">
          <VoiceAckToggle toast={toast} />
        </Card>
        <ChangePasswordCard toast={toast} />
        <VoiceEnrollmentCard toast={toast} />
        <KioskPinCard toast={toast} />
        <KioskAgendaConsentCard toast={toast} />
        <ConfirmMutationsToggle toast={toast} />
        <DevModeToggle toast={toast} />
        <DefaultDocVisibilityChips toast={toast} />
        <SuggestionEngineCard toast={toast} />

        <div className="flex justify-end">
          <button
            onClick={save}
            disabled={busy}
            className={cn(
              "px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-1.5 transition",
              "bg-gradient-to-r from-violet-500 to-blue-500 hover:from-violet-600 hover:to-blue-600 text-white shadow-md",
              busy && "opacity-60 cursor-wait",
            )}
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            Save changes
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Theme toggle ─────────────────────────────────────────────────────
// Three-state segmented picker: Light · Dark · System.
// Persisted in localStorage("yorik_theme"). index.html applies the saved
// value before first paint so there's no flash. We mirror the runtime
// flip here by toggling the `.dark` class on <html>.

type Theme = "light" | "dark" | "system";

function applyTheme(theme: Theme) {
  let dark: boolean;
  if (theme === "system") {
    dark = !(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches);
  } else {
    dark = theme === "dark";
  }
  document.documentElement.classList.toggle("dark", dark);
}

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      const saved = localStorage.getItem("yorik_theme");
      if (saved === "light" || saved === "dark" || saved === "system") return saved;
    } catch {}
    return "system";
  });

  // Re-apply when the OS preference changes — only matters in "system" mode.
  useEffect(() => {
    if (theme !== "system" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const handler = () => applyTheme("system");
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [theme]);

  function pick(next: Theme) {
    setTheme(next);
    try { localStorage.setItem("yorik_theme", next); } catch {}
    applyTheme(next);
  }

  const options: { id: Theme; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
    { id: "light",  label: "Light",  icon: Sun },
    { id: "dark",   label: "Dark",   icon: Moon },
    { id: "system", label: "System", icon: Monitor },
  ];

  return (
    <Card title="Appearance">
      <div className="flex items-center justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">Theme</div>
          <p className="text-xs text-muted-foreground mt-1">
            Light, dark, or follow your operating system. Saved per device.
          </p>
        </div>
        <div className="inline-flex rounded-md border border-border bg-card p-0.5 shrink-0">
          {options.map(o => {
            const Icon = o.icon;
            const active = theme === o.id;
            return (
              <button
                key={o.id}
                onClick={() => pick(o.id)}
                className={cn(
                  "inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded transition",
                  active
                    ? "bg-violet-500 text-white shadow-sm"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted",
                )}
                aria-pressed={active}
                title={o.label}
              >
                <Icon className="w-3.5 h-3.5" />
                {o.label}
              </button>
            );
          })}
        </div>
      </div>
    </Card>
  );
}


// ─── Instant ack toggle ──────────────────────────────────────────────
// When ON (default), Yorik plays a short "klar, Moment" / "on it"
// sound the moment STT finishes — before the LLM call — so voice
// feels like a natural conversation. The phrase is random per turn
// (6 per language). User can switch it off here.

function VoiceAckToggle({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.get<{ logged_in: boolean; user?: any }>("/api/auth/me");
        setEnabled(r.user?.voice_ack_enabled !== false);
      } catch {
        setEnabled(true);
      }
    })();
  }, []);

  async function toggle() {
    if (enabled === null) return;
    const next = !enabled;
    setSaving(true);
    try {
      await api.patch("/api/profile/voice-ack", { enabled: next });
      setEnabled(next);
      toast(
        next
          ? "Instant confirmation sounds ON"
          : "Instant confirmation sounds OFF",
        "success",
      );
    } catch (e: any) {
      toast(e.message || "Failed to save", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex-1">
        <div className="text-sm font-medium">Instant confirmation sound</div>
        <p className="text-xs text-muted-foreground mt-0.5">
          Yorik says "on it" or similar the moment it hears you,
          before answering. Makes the conversation feel natural — random
          phrase per turn so it doesn't sound robotic.
        </p>
      </div>
      <button
        onClick={toggle}
        disabled={enabled === null || saving}
        className={cn(
          "shrink-0 relative inline-flex h-6 w-11 items-center rounded-full transition",
          enabled ? "bg-violet-500" : "bg-muted",
          (enabled === null || saving) && "opacity-60 cursor-wait",
        )}
        aria-pressed={!!enabled}
      >
        <span
          className={cn(
            "inline-block h-4 w-4 transform rounded-full bg-white transition",
            enabled ? "translate-x-6" : "translate-x-1",
          )}
        />
      </button>
    </div>
  );
}


// ─── Self-service password change (Profile tab) ──────────────────────
// Powers the missing UI for /api/auth/change-password. Backend requires
// the current password + a new ≥8-char password. Admin-side reset of
// OTHER users' passwords is the separate ResetPasswordModal in the
// Users tab (calls /api/users/{id}/reset-password); this card is for
// "change my own password".

function ChangePasswordCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [show, setShow] = useState(false);
  const [saving, setSaving] = useState(false);

  const newTooShort = newPw.length > 0 && newPw.length < 8;
  const mismatch    = confirmPw.length > 0 && confirmPw !== newPw;
  const canSubmit   = !saving && currentPw.length > 0
                       && newPw.length >= 8 && confirmPw === newPw;

  async function submit() {
    if (!canSubmit) return;
    setSaving(true);
    try {
      await api.post("/api/auth/change-password", {
        current_password: currentPw,
        new_password:     newPw,
      });
      setCurrentPw(""); setNewPw(""); setConfirmPw("");
      toast("Password changed. Your other sessions stay active.", "success");
    } catch (e: any) {
      // 401 = current password wrong; 400 = new too short / other validation
      const msg = e?.message || "Couldn't change password";
      toast(msg.includes("401") || msg.toLowerCase().includes("current")
        ? "Current password is incorrect"
        : msg, "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title="Password">
      <div className="mb-3 flex items-start gap-2">
        <KeyRound className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">Change your password</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Requires your current password. Minimum 8 characters.
          </p>
        </div>
        <button
          onClick={() => setShow(s => !s)}
          className="text-xs text-muted-foreground hover:text-foreground transition shrink-0"
          type="button"
        >
          {show ? "Hide" : "Show"}
        </button>
      </div>
      <div className="space-y-2.5">
        <Field label="Current password">
          <input
            type={show ? "text" : "password"}
            value={currentPw}
            onChange={e => setCurrentPw(e.target.value)}
            autoComplete="current-password"
            className={inputClass}
          />
        </Field>
        <Field label="New password">
          <input
            type={show ? "text" : "password"}
            value={newPw}
            onChange={e => setNewPw(e.target.value)}
            autoComplete="new-password"
            className={inputClass}
          />
        </Field>
        {newTooShort && (
          <p className="text-[11px] text-amber-600 dark:text-amber-500 -mt-1.5">
            Too short — needs ≥8 characters.
          </p>
        )}
        <Field label="Confirm new password">
          <input
            type={show ? "text" : "password"}
            value={confirmPw}
            onChange={e => setConfirmPw(e.target.value)}
            autoComplete="new-password"
            className={inputClass}
            onKeyDown={e => { if (e.key === "Enter" && canSubmit) submit(); }}
          />
        </Field>
        {mismatch && (
          <p className="text-[11px] text-amber-600 dark:text-amber-500 -mt-1.5">
            Doesn't match the new password above.
          </p>
        )}
      </div>
      <div className="flex justify-end mt-4">
        <button
          onClick={submit}
          disabled={!canSubmit}
          className={cn(
            "px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-1.5 transition",
            "bg-gradient-to-r from-violet-500 to-fuchsia-500 hover:from-violet-600 hover:to-fuchsia-600 text-white shadow-md",
            "disabled:opacity-60 disabled:cursor-not-allowed",
          )}
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          Change password
        </button>
      </div>
    </Card>
  );
}


// ─── Kiosk PIN ───────────────────────────────────────────────────────
// 4-digit PIN used by household members to identify themselves on a
// kiosk tablet when voice ID can't match them. The PIN is NEVER used
// on regular browser logins — full password is still required there.
// See backend/auth_sessions.set_pin + /api/auth/pin-switch.
//
// Each user sets their own PIN here (the endpoint uses their session).
// Households without enrolled voices can use PIN as the primary
// identification method on the wall.

function KioskPinCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const [meHasPin, setMeHasPin] = useState<boolean | null>(null);
  const [pinSetAt, setPinSetAt] = useState<string | null>(null);
  const [pin, setPin]           = useState("");
  const [pin2, setPin2]         = useState("");
  const [show, setShow]         = useState(false);
  const [saving, setSaving]     = useState(false);

  // Bootstrap: peek at user_profiles via /api/auth/me's extended shape
  // (pin_set_at lands as part of the profile row).
  useEffect(() => {
    (async () => {
      try {
        const me = await api.get<any>("/api/auth/me");
        const u = me?.user || {};
        setMeHasPin(Boolean(u.pin_set_at));
        setPinSetAt(u.pin_set_at || null);
      } catch {
        setMeHasPin(false);
      }
    })();
  }, []);

  const isValid    = pin.length === 4 && /^\d{4}$/.test(pin);
  const matches    = pin === pin2;
  const canSubmit  = !saving && isValid && matches;

  async function save() {
    if (!canSubmit) return;
    setSaving(true);
    try {
      await api.post("/api/profile/pin", { pin });
      setPin(""); setPin2("");
      setMeHasPin(true);
      setPinSetAt(new Date().toISOString());
      toast("Kiosk PIN set", "success");
    } catch (e: any) {
      toast(e?.message || "Couldn't set PIN", "error");
    } finally {
      setSaving(false);
    }
  }

  async function clear() {
    if (!confirm("Remove your kiosk PIN? You won't be able to identify yourself on a kiosk until you set a new one.")) return;
    setSaving(true);
    try {
      await api.delete("/api/profile/pin");
      setMeHasPin(false);
      setPinSetAt(null);
      toast("Kiosk PIN cleared", "success");
    } catch (e: any) {
      toast(e?.message || "Couldn't clear PIN", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title="Kiosk PIN">
      <div className="mb-3 flex items-start gap-2">
        <KeyRound className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">4-digit PIN for the household wall</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Used on kiosk tablets when voice ID can't match you. Never
            used on regular browser logins (your password stays the
            only way in there).
          </p>
        </div>
      </div>

      {meHasPin === true && (
        <div className="mb-3 rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-700 dark:text-emerald-400 flex items-center justify-between gap-2">
          <span>
            PIN set
            {pinSetAt ? ` on ${new Date(pinSetAt).toLocaleDateString()}` : ""}.
          </span>
          <button
            onClick={clear}
            disabled={saving}
            className="text-[11px] underline hover:no-underline disabled:opacity-50"
          >
            Clear PIN
          </button>
        </div>
      )}

      <div className="space-y-2.5">
        <Field label={meHasPin ? "New PIN (4 digits)" : "PIN (4 digits)"}>
          <input
            type={show ? "text" : "password"}
            inputMode="numeric"
            pattern="[0-9]*"
            maxLength={4}
            autoComplete="off"
            value={pin}
            onChange={e => setPin(e.target.value.replace(/\D/g, "").slice(0, 4))}
            className={inputClass}
          />
        </Field>
        <Field label="Confirm PIN">
          <input
            type={show ? "text" : "password"}
            inputMode="numeric"
            pattern="[0-9]*"
            maxLength={4}
            autoComplete="off"
            value={pin2}
            onChange={e => setPin2(e.target.value.replace(/\D/g, "").slice(0, 4))}
            className={inputClass}
          />
        </Field>
        {pin.length > 0 && pin.length < 4 && (
          <p className="text-[11px] text-amber-600 dark:text-amber-500 -mt-1.5">
            PINs are exactly 4 digits.
          </p>
        )}
        {pin2.length === 4 && !matches && (
          <p className="text-[11px] text-amber-600 dark:text-amber-500 -mt-1.5">
            PINs don't match.
          </p>
        )}
      </div>

      <div className="flex items-center justify-end gap-2 mt-4">
        <button
          onClick={() => setShow(s => !s)}
          className="text-xs text-muted-foreground hover:text-foreground transition"
          type="button"
        >
          {show ? "Hide digits" : "Show digits"}
        </button>
        <button
          onClick={save}
          disabled={!canSubmit}
          className={cn(
            "px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-1.5 transition",
            "bg-gradient-to-r from-blue-500 to-cyan-500 hover:from-blue-600 hover:to-cyan-600 text-white shadow-md",
            "disabled:opacity-60 disabled:cursor-not-allowed",
          )}
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          {meHasPin ? "Update PIN" : "Set PIN"}
        </button>
      </div>
    </Card>
  );
}


// ─── Voice enrollment (kiosk speaker ID) ────────────────────────────
// Records a short sample, POSTs to /api/voice-profile/{my_id}/enroll
// so the kiosk wall can identify the user by voice without a PIN
// round-trip. Each user enrolls their OWN profile — backend gates by
// profile_id == session user (admin can override for someone else).
//
// Recording lives entirely in MediaRecorder + getUserMedia — no
// streaming, no transcoding. We send the raw blob (typically webm/
// opus) and let voice_id handle decoding via ffmpeg/torchaudio.

function VoiceEnrollmentCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const auth = useAuth();
  const myId = auth.user.id;

  // Recording target — long enough to give ECAPA a stable embedding,
  // short enough that nobody dreads the prompt. Backend MIN is 2s.
  const TARGET_SECONDS = 10;
  // Sample prompt — phonetically varied so the embedding covers
  // common vowels + consonants. Households can re-record any time
  // if they want to try a different prompt.
  const PROMPT = "Hey Yorik, today is a good day. I'd like to check what's on my calendar, then read the news. Thank you.";

  const [enrolled,    setEnrolled]    = useState<boolean | null>(null);
  const [recording,   setRecording]   = useState(false);
  const [remaining,   setRemaining]   = useState(TARGET_SECONDS);
  const [uploading,   setUploading]   = useState(false);
  const [error,       setError]       = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef   = useRef<Blob[]>([]);
  const timerRef    = useRef<number | null>(null);

  // Fetch enrollment status. /api/voice-profiles returns every user
  // with an `enrolled` flag — we filter to the current user.
  const refresh = useCallback(async () => {
    try {
      const all = await api.get<any[]>("/api/voice-profiles");
      const mine = all.find(p => p.id === myId);
      setEnrolled(Boolean(mine?.enrolled));
    } catch {
      setEnrolled(null);
    }
  }, [myId]);

  useEffect(() => { refresh(); }, [refresh]);

  async function startRecording() {
    setError(null);
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("This browser doesn't support voice recording. Try Chrome / Edge / Safari.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
      };
      rec.onstop = async () => {
        // Always release the mic before doing anything async — leaving
        // it open even briefly turns the indicator dot red on most OSes
        // and freaks people out.
        stream.getTracks().forEach(t => t.stop());
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || "audio/webm" });
        chunksRef.current = [];
        await upload(blob);
      };
      rec.start();
      recorderRef.current = rec;
      setRecording(true);
      setRemaining(TARGET_SECONDS);
      // Countdown + auto-stop
      const startedAt = Date.now();
      timerRef.current = window.setInterval(() => {
        const elapsed = (Date.now() - startedAt) / 1000;
        const left = Math.max(0, TARGET_SECONDS - elapsed);
        setRemaining(Math.ceil(left));
        if (left <= 0) {
          if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
          if (recorderRef.current?.state === "recording") {
            recorderRef.current.stop();
          }
          setRecording(false);
        }
      }, 150);
    } catch (err: any) {
      const msg = err?.name === "NotAllowedError"
        ? "Microphone permission denied. Allow it for this page and try again."
        : (err?.message || "Couldn't open the microphone");
      setError(msg);
    }
  }

  function stopRecording() {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    if (recorderRef.current?.state === "recording") {
      recorderRef.current.stop();
    }
    setRecording(false);
  }

  async function upload(blob: Blob) {
    setUploading(true);
    try {
      const fd = new FormData();
      fd.append("audio", blob, "enroll.webm");
      // Bypass the api helper here because it stringifies bodies; we
      // need a multipart upload with the FormData.
      const r = await fetch(`/api/voice-profile/${myId}/enroll`, {
        method: "POST",
        body: fd,
        credentials: "include",
      });
      if (!r.ok) {
        const text = await r.text();
        try {
          const j = JSON.parse(text);
          throw new Error(j.detail || j.message || text);
        } catch {
          throw new Error(text || `HTTP ${r.status}`);
        }
      }
      toast("Voice enrolled — Yorik will recognise you on the kiosk now", "success");
      await refresh();
    } catch (err: any) {
      const msg = err?.message || "Enrollment failed";
      setError(msg);
      toast(`Enrollment failed: ${msg}`, "error");
    } finally {
      setUploading(false);
    }
  }

  async function clearEnrollment() {
    if (!confirm("Remove your voice enrollment? Kiosk will fall back to PIN until you re-enroll.")) return;
    setUploading(true);
    try {
      const r = await fetch(`/api/voice-profile/${myId}/enrollment`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
      toast("Voice enrollment cleared", "success");
      await refresh();
    } catch (err: any) {
      toast(err?.message || "Couldn't clear enrollment", "error");
    } finally {
      setUploading(false);
    }
  }

  return (
    <Card title="Kiosk voice">
      <div className="mb-3 flex items-start gap-2">
        <Mic className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">Enroll your voice for the household wall</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Records a short sample so Yorik can recognise you on a
            kiosk tablet without you typing your PIN. Used ONLY on
            kiosks — regular browser sessions never run voice
            recognition.
          </p>
        </div>
      </div>

      {enrolled === true && !recording && !uploading && (
        <div className="mb-3 rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-700 dark:text-emerald-400 flex items-center justify-between gap-2">
          <span>Voice enrolled. The kiosk will recognise you silently.</span>
          <button
            onClick={clearEnrollment}
            disabled={uploading}
            className="text-[11px] underline hover:no-underline disabled:opacity-50"
          >
            Clear
          </button>
        </div>
      )}

      {error && (
        <div className="rounded-md border border-rose-500/30 bg-rose-500/5 px-3 py-2 text-xs text-rose-700 dark:text-rose-400 mb-3">
          {error}
        </div>
      )}

      <div className="rounded-md border border-violet-500/20 bg-violet-500/5 px-3 py-2 text-xs text-foreground/80 mb-3 italic">
        Read aloud: <span className="not-italic">"{PROMPT}"</span>
      </div>

      <div className="flex items-center gap-3">
        {!recording && !uploading && (
          <button
            onClick={startRecording}
            className={cn(
              "px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-1.5 transition",
              "bg-gradient-to-r from-violet-500 to-rose-500 hover:from-violet-600 hover:to-rose-600 text-white shadow-md",
            )}
          >
            <Mic className="w-4 h-4" />
            {enrolled ? "Re-enroll voice" : "Record voice sample"}
          </button>
        )}

        {recording && (
          <button
            onClick={stopRecording}
            className="px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-1.5 bg-rose-500 text-white shadow-md hover:bg-rose-600"
          >
            <Square className="w-4 h-4" />
            Stop · {remaining}s left
          </button>
        )}

        {uploading && (
          <span className="text-xs text-muted-foreground inline-flex items-center gap-1.5">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Enrolling…
          </span>
        )}
      </div>

      {recording && (
        <div className="mt-3 h-2 rounded-full bg-muted overflow-hidden">
          <div
            className="h-full bg-gradient-to-r from-violet-500 to-rose-500 transition-all"
            style={{ width: `${((TARGET_SECONDS - remaining) / TARGET_SECONDS) * 100}%` }}
          />
        </div>
      )}
    </Card>
  );
}


// ─── Whisper STT model picker (LLM tab) ──────────────────────────────
// Global backend setting — the active Whisper model affects every
// user's voice request, persists to config.env as HOMEOS_WHISPER_MODEL,
// and the PATCH endpoint requires admin. Lives in Settings → LLM
// because it's a model choice with the same shape as the chat-LLM
// endpoint: pick a model, switching downloads it on next use.
// The per-user "instant confirmation sound" toggle is its own
// component (VoiceAckToggle) and stays in Profile.

interface WhisperModel {
  id: string;
  label: string;
  size_mb: number;
  ms_short: number;
  blurb: string;
}

interface STTBackend {
  id: "whisper" | "groq" | "openai-compatible";
  label: string;
  blurb: string;
  requires_key: boolean;
  default_url: string;
  default_model: string;
}

interface STTConfigResponse {
  stt_model: string;
  catalogue: WhisperModel[];
  stt_backend: STTBackend["id"];
  stt_url: string;
  stt_api_key_set: boolean;
  stt_model_name: string;
  backends: STTBackend[];
}

function STTConfigCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  // Whisper model picker (unchanged surface — admin can still swap
  // tiny ↔ turbo etc, even when a cloud engine is selected, because
  // Whisper stays the silent fallback).
  const [currentModel, setCurrentModel] = useState<string | null>(null);
  const [catalogue, setCatalogue] = useState<WhisperModel[]>([]);
  const [busyModel, setBusyModel] = useState<string | null>(null);

  // Engine picker.
  const [backend, setBackend] = useState<STTBackend["id"]>("whisper");
  const [backends, setBackends] = useState<STTBackend[]>([]);
  const [url, setUrl] = useState<string>("");
  const [modelName, setModelName] = useState<string>("");
  const [apiKeySet, setApiKeySet] = useState<boolean>(false);
  const [apiKeyInput, setApiKeyInput] = useState<string>("");
  const [savingBackend, setSavingBackend] = useState(false);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.get<STTConfigResponse>("/api/voice/config");
        setCurrentModel(r.stt_model);
        setCatalogue(r.catalogue || []);
        setBackend(r.stt_backend);
        setBackends(r.backends || []);
        setUrl(r.stt_url || "");
        setModelName(r.stt_model_name || "");
        setApiKeySet(!!r.stt_api_key_set);
      } catch (e: any) {
        toast(`Couldn't load voice config: ${e.message}`, "error");
      }
    })();
  }, [toast]);

  async function pickModel(id: string) {
    if (id === currentModel) return;
    setBusyModel(id);
    try {
      await api.patch("/api/voice/config", { stt_model: id });
      setCurrentModel(id);
      const meta = catalogue.find(m => m.id === id);
      toast(
        `Switched to ${meta?.label || id}. ${meta && meta.size_mb > 200
          ? `First voice request will download the model (~${meta.size_mb} MB).`
          : "Active on next voice request."}`,
        "success",
      );
    } catch (e: any) {
      toast(e.message || "Switch failed", "error");
    } finally {
      setBusyModel(null);
    }
  }

  // Switch engine. For cloud engines we PRE-FILL URL + model name
  // from the catalogue so the user only has to paste a key.
  function selectBackend(id: STTBackend["id"]) {
    if (id === backend) return;
    setBackend(id);
    const meta = backends.find(b => b.id === id);
    if (meta) {
      if (!url || backend === "whisper") setUrl(meta.default_url);
      if (!modelName || backend === "whisper") setModelName(meta.default_model);
    }
    // Don't auto-save yet — wait for the user to paste a key and
    // click Save. This avoids switching the active backend before
    // it's actually usable.
    if (id === "whisper") {
      // Whisper has no key/url to fill; flip the active backend
      // immediately so the previous cloud config doesn't keep
      // serving requests.
      saveBackend({ stt_backend: "whisper" }, "Switched to local Whisper.");
    }
  }

  async function saveBackend(payload: Record<string, unknown>, successMsg: string) {
    setSavingBackend(true);
    try {
      const r = await api.patch<{ ok: boolean; stt_api_key_set: boolean }>(
        "/api/voice/config", payload,
      );
      setApiKeySet(!!r.stt_api_key_set);
      setApiKeyInput("");
      toast(successMsg, "success");
    } catch (e: any) {
      toast(e.message || "Save failed", "error");
    } finally {
      setSavingBackend(false);
    }
  }

  async function saveCloudConfig() {
    const meta = backends.find(b => b.id === backend);
    const trimmedKey = apiKeyInput.trim();
    if (meta?.requires_key && !trimmedKey && !apiKeySet) {
      toast("Paste an API key first.", "error");
      return;
    }
    const payload: Record<string, unknown> = {
      stt_backend:    backend,
      stt_url:        url.trim(),
      stt_model_name: modelName.trim(),
    };
    if (trimmedKey) payload.stt_api_key = trimmedKey;
    await saveBackend(payload, `Saved. ${meta?.label || backend} is now active.`);
  }

  async function testConnection() {
    setTesting(true);
    try {
      const trimmedKey = apiKeyInput.trim();
      const r = await api.post<{ ok: boolean; error?: string; note?: string }>(
        "/api/voice/test-connection",
        {
          backend,
          url: url.trim() || undefined,
          model_name: modelName.trim() || undefined,
          // If the user typed a key, test with it; otherwise test
          // with the stored one. We never send the stored key
          // back; the server reads its own state when api_key is
          // omitted.
          ...(trimmedKey ? { api_key: trimmedKey } : {}),
        },
      );
      if (r.ok) {
        toast(r.note || "Connection OK — endpoint accepted the test audio.", "success");
      } else {
        toast(`Connection failed: ${r.error || "unknown error"}`, "error");
      }
    } catch (e: any) {
      toast(e.message || "Test failed", "error");
    } finally {
      setTesting(false);
    }
  }

  const currentBackendMeta = backends.find(b => b.id === backend);
  const isCloud = backend !== "whisper";

  return (
    <Card title="Speech-to-text">
      <div className="mb-3 flex items-start gap-2">
        <Mic className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">Transcription engine</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Applies to every user — global backend setting.
            Local Whisper keeps your audio on this machine; cloud engines send
            audio to the provider but are faster and more accurate.
          </p>
        </div>
      </div>

      {/* Engine picker */}
      <div className="space-y-1.5 mb-4">
        {backends.map(b => {
          const active = backend === b.id;
          const Icon = b.id === "whisper" ? HardDrive : Globe;
          return (
            <button
              key={b.id}
              onClick={() => selectBackend(b.id)}
              disabled={savingBackend}
              className={cn(
                "w-full text-left border rounded-md px-3 py-2 transition",
                active
                  ? "border-violet-500 bg-violet-500/5"
                  : "border-border bg-card hover:bg-muted hover:border-violet-500/30",
              )}
            >
              <div className="flex items-center gap-2 min-w-0">
                {active
                  ? <CheckCircle2 className="w-4 h-4 text-violet-500 shrink-0" />
                  : <div className="w-4 h-4 rounded-full border border-border shrink-0" />}
                <Icon className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                <span className="font-medium text-sm">{b.label}</span>
                {b.requires_key && (
                  <span className="text-[10px] text-muted-foreground font-mono">
                    API key
                  </span>
                )}
              </div>
              <div className="text-[11px] text-muted-foreground mt-1 ml-6">
                {b.blurb}
              </div>
            </button>
          );
        })}
      </div>

      {/* Cloud-engine settings (URL + key + model + test) */}
      {isCloud && currentBackendMeta && (
        <div className="space-y-2.5 mb-4 border border-border rounded-md p-3 bg-muted/30">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-3.5 h-3.5 text-amber-500 mt-0.5 shrink-0" />
            <p className="text-[11px] text-muted-foreground">
              Audio is uploaded to <span className="font-mono">{currentBackendMeta.label}</span> for transcription.
              Yorik falls back to local Whisper automatically if the cloud is unreachable.
            </p>
          </div>

          <div>
            <label className="block text-[11px] text-muted-foreground mb-1">Endpoint URL</label>
            <input
              type="text"
              value={url}
              onChange={e => setUrl(e.target.value)}
              placeholder={currentBackendMeta.default_url}
              readOnly={backend === "groq"}
              className={cn(
                "w-full text-xs font-mono px-2 py-1.5 rounded border border-border bg-background",
                backend === "groq" && "opacity-60 cursor-not-allowed",
              )}
            />
          </div>

          <div>
            <label className="block text-[11px] text-muted-foreground mb-1">Model</label>
            <input
              type="text"
              value={modelName}
              onChange={e => setModelName(e.target.value)}
              placeholder={currentBackendMeta.default_model}
              className="w-full text-xs font-mono px-2 py-1.5 rounded border border-border bg-background"
            />
          </div>

          <div>
            <label className="block text-[11px] text-muted-foreground mb-1">
              API key {apiKeySet && !apiKeyInput && (
                <span className="text-emerald-600 font-mono">— saved</span>
              )}
            </label>
            <input
              type="password"
              value={apiKeyInput}
              onChange={e => setApiKeyInput(e.target.value)}
              placeholder={apiKeySet ? "•••••••• (key on file — paste a new one to replace)" : "Paste your API key"}
              autoComplete="off"
              spellCheck={false}
              className="w-full text-xs font-mono px-2 py-1.5 rounded border border-border bg-background"
            />
          </div>

          <div className="flex items-center gap-2 pt-1">
            <button
              onClick={saveCloudConfig}
              disabled={savingBackend}
              className="text-xs px-3 py-1.5 rounded bg-violet-500 text-white hover:bg-violet-600 disabled:opacity-60 inline-flex items-center gap-1.5"
            >
              {savingBackend
                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                : <Save className="w-3.5 h-3.5" />}
              Save
            </button>
            <button
              onClick={testConnection}
              disabled={testing || (!apiKeySet && !apiKeyInput)}
              className="text-xs px-3 py-1.5 rounded border border-border bg-card hover:bg-muted disabled:opacity-60 inline-flex items-center gap-1.5"
            >
              {testing
                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                : <Plug className="w-3.5 h-3.5" />}
              Test connection
            </button>
          </div>
        </div>
      )}

      {/* Whisper model picker — always visible. Drives the local
          backend and the fallback path for cloud engines. */}
      <div className="border-t border-border pt-3 mt-1">
        <div className="flex items-start gap-2 mb-2">
          <HardDrive className="w-3.5 h-3.5 text-muted-foreground mt-0.5 shrink-0" />
          <div className="flex-1">
            <div className="text-xs font-medium">Local Whisper model</div>
            <p className="text-[11px] text-muted-foreground mt-0.5">
              {isCloud
                ? "Used as fallback when the cloud engine is unreachable. Bigger = better fallback quality."
                : "Bigger = more accurate, slower, larger download. Switching downloads the new model on the next voice request."}
            </p>
          </div>
        </div>
        <div className="space-y-1.5">
          {catalogue.map(m => {
            const active = currentModel === m.id;
            const isBusy = busyModel === m.id;
            return (
              <button
                key={m.id}
                onClick={() => pickModel(m.id)}
                disabled={busyModel !== null}
                className={cn(
                  "w-full text-left border rounded-md px-3 py-2 transition",
                  active
                    ? "border-violet-500 bg-violet-500/5"
                    : "border-border bg-card hover:bg-muted hover:border-violet-500/30",
                  isBusy && "opacity-60 cursor-wait",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    {active
                      ? <CheckCircle2 className="w-4 h-4 text-violet-500 shrink-0" />
                      : isBusy
                      ? <Loader2 className="w-4 h-4 animate-spin shrink-0" />
                      : <div className="w-4 h-4 rounded-full border border-border shrink-0" />}
                    <span className="font-medium text-sm">{m.label}</span>
                    <span className="text-[10px] text-muted-foreground font-mono">{m.id}</span>
                  </div>
                  <div className="text-[10px] text-muted-foreground text-right tabular-nums shrink-0">
                    ~{m.size_mb} MB · {m.ms_short < 1000 ? `${m.ms_short}ms` : `${(m.ms_short/1000).toFixed(1)}s`}
                  </div>
                </div>
                <div className="text-[11px] text-muted-foreground mt-1 ml-6">{m.blurb}</div>
              </button>
            );
          })}
        </div>
      </div>
    </Card>
  );
}


// ─── Confirm mutations toggle ─────────────────────────────────────────
// Beta safety net: when ON, LLM-initiated create/update/delete actions
// show a confirmation modal before they happen. Decision feeds the
// per-model quality dashboard (Settings → Quality).

// ─── Kiosk agenda consent — show MY appointments on the household wall ───

function KioskAgendaConsentCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.get<{ consent: boolean }>("/api/users/me/kiosk-agenda-consent");
        setEnabled(!!r.consent);
      } catch {
        setEnabled(false);
      }
    })();
  }, []);

  async function toggle() {
    if (enabled === null) return;
    const next = !enabled;
    setSaving(true);
    try {
      await api.patch("/api/users/me/kiosk-agenda-consent", { consent: next });
      setEnabled(next);
      toast(next
        ? "Your appointments will appear on the household wall."
        : "Your appointments are no longer on the wall.",
        "success");
    } catch (e: any) {
      toast(e.message || "Failed to save", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title="Household wall">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">Show my appointments on the household wall</div>
          <p className="text-xs text-muted-foreground mt-1">
            When ON, swiping right on the kitchen tablet's photo wall reveals
            today's agenda — including YOUR events alongside everyone else
            who opted in. Each event is labeled with the owner's name. Off
            by default; nobody can see your appointments on the wall without
            this toggle.
          </p>
        </div>
        <button
          onClick={toggle}
          disabled={enabled === null || saving}
          className={cn(
            "shrink-0 relative inline-flex h-6 w-11 items-center rounded-full transition",
            enabled ? "bg-violet-500" : "bg-muted",
            (enabled === null || saving) && "opacity-60 cursor-wait",
          )}
          aria-pressed={!!enabled}
        >
          <span
            className={cn(
              "inline-block h-4 w-4 transform rounded-full bg-white transition",
              enabled ? "translate-x-6" : "translate-x-1",
            )}
          />
        </button>
      </div>
    </Card>
  );
}


function ConfirmMutationsToggle({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.get<{ logged_in: boolean; user?: any }>("/api/auth/me");
        setEnabled(!!r.user?.confirm_mutations);
      } catch {
        setEnabled(true);  // safe default
      }
    })();
  }, []);

  async function toggle() {
    if (enabled === null) return;
    const next = !enabled;
    setSaving(true);
    try {
      await api.patch("/api/profile/confirm-mutations", { enabled: next });
      setEnabled(next);
      toast(next
        ? "Confirmations ON — LLM actions will show a modal"
        : "Confirmations OFF — LLM actions run immediately",
        "success");
    } catch (e: any) {
      toast(e.message || "Failed to save", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title="Beta safety">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">Confirm LLM actions before they happen</div>
          <p className="text-xs text-muted-foreground mt-1">
            When ON, the assistant shows a modal before creating, updating, or deleting
            anything. Your click also feeds the per-model success rate in{" "}
            <strong>Quality</strong> — so we know which LLMs are reliable. We recommend
            keeping this ON during beta. Turn off once you trust the model.
          </p>
        </div>
        <button
          onClick={toggle}
          disabled={enabled === null || saving}
          className={cn(
            "shrink-0 relative inline-flex h-6 w-11 items-center rounded-full transition",
            enabled ? "bg-violet-500" : "bg-muted",
            (enabled === null || saving) && "opacity-60 cursor-wait",
          )}
          aria-pressed={!!enabled}
        >
          <span
            className={cn(
              "inline-block h-4 w-4 transform rounded-full bg-white transition",
              enabled ? "translate-x-6" : "translate-x-1",
            )}
          />
        </button>
      </div>
    </Card>
  );
}

// ─── Suggestion engine ─ master toggle + per-source switches ───────

function SuggestionEngineCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [sources, setSources] = useState<Record<string, boolean>>({ email: true });
  const [saving, setSaving] = useState<"" | "master" | string>("");

  useEffect(() => {
    (async () => {
      try {
        const r = await api.get<{ suggestions_enabled: boolean; sources: Record<string, boolean> }>(
          "/api/suggestions/settings",
        );
        setEnabled(!!r.suggestions_enabled);
        setSources(r.sources || { email: true });
      } catch {
        setEnabled(false);
      }
    })();
  }, []);

  async function toggleMaster() {
    if (enabled === null) return;
    const next = !enabled;
    setSaving("master");
    try {
      await api.post("/api/suggestions/settings", { suggestions_enabled: next });
      setEnabled(next);
      toast(next
        ? "Suggestions ON — Yorik will analyse opted-in contacts' messages"
        : "Suggestions OFF — no analysis, no cards",
        "success");
    } catch (e: any) {
      toast(e.message || "Failed to save", "error");
    } finally {
      setSaving("");
    }
  }

  async function toggleSource(key: string) {
    const next = { ...sources, [key]: !sources[key] };
    setSaving(key);
    try {
      await api.post("/api/suggestions/settings", { sources: next });
      setSources(next);
    } catch (e: any) {
      toast(e.message || "Failed to save", "error");
    } finally {
      setSaving("");
    }
  }

  const KNOWN_SOURCES: Array<{ key: string; label: string; hint?: string }> = [
    { key: "email", label: "Email",    hint: "Inbound messages from opted-in contacts" },
    { key: "wa",    label: "WhatsApp", hint: "Coming soon" },
  ];

  return (
    <Card title="Suggestions">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">Let Yorik suggest actions</div>
          <p className="text-xs text-muted-foreground mt-1">
            When ON, Yorik reads new messages from contacts you've opted in and proposes
            one-click actions (reply drafts, meeting slots). Privacy: only contacts with
            "Yorik assist" enabled are analysed. Turn this OFF and nothing is sent to the LLM.
          </p>
        </div>
        <button
          onClick={toggleMaster}
          disabled={enabled === null || saving === "master"}
          className={cn(
            "shrink-0 relative inline-flex h-6 w-11 items-center rounded-full transition",
            enabled ? "bg-violet-500" : "bg-muted",
            (enabled === null || saving === "master") && "opacity-60 cursor-wait",
          )}
          aria-pressed={!!enabled}
        >
          <span
            className={cn(
              "inline-block h-4 w-4 transform rounded-full bg-white transition",
              enabled ? "translate-x-6" : "translate-x-1",
            )}
          />
        </button>
      </div>
      {enabled && (
        <div className="mt-4 pt-3 border-t border-border space-y-2">
          <div className="text-xs uppercase tracking-wider text-muted-foreground font-medium">
            Sources
          </div>
          {KNOWN_SOURCES.map((src) => (
            <div key={src.key} className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-medium capitalize">{src.label}</div>
                {src.hint && <div className="text-xs text-muted-foreground">{src.hint}</div>}
              </div>
              <button
                onClick={() => toggleSource(src.key)}
                disabled={src.key === "wa" || saving === src.key}
                className={cn(
                  "shrink-0 relative inline-flex h-5 w-9 items-center rounded-full transition",
                  sources[src.key] ? "bg-violet-500" : "bg-muted",
                  (src.key === "wa" || saving === src.key) && "opacity-50 cursor-not-allowed",
                )}
                aria-pressed={!!sources[src.key]}
              >
                <span className={cn(
                  "inline-block h-3.5 w-3.5 transform rounded-full bg-white transition",
                  sources[src.key] ? "translate-x-5" : "translate-x-1",
                )} />
              </button>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}


// ─── Dev-mode toggle — shows the per-iteration agent trace under each chat reply ──

function DevModeToggle({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.get<{ logged_in: boolean; user?: any }>("/api/auth/me");
        setEnabled(!!r.user?.dev_mode);
      } catch {
        setEnabled(false);
      }
    })();
  }, []);

  async function toggle() {
    if (enabled === null) return;
    const next = !enabled;
    setSaving(true);
    try {
      await api.patch("/api/profile/dev-mode", { enabled: next });
      setEnabled(next);
      toast(next
        ? "Dev mode ON — agent trace will appear under each reply"
        : "Dev mode OFF — chat is clean again",
        "success");
    } catch (e: any) {
      toast(e.message || "Failed to save", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title="Developer mode">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">Show agent trace under each reply</div>
          <p className="text-xs text-muted-foreground mt-1">
            Adds a collapsible <span className="font-mono opacity-80">▼ Debug</span> caret below every Yorik reply.
            Expand it to see iterations, tool calls (with arguments + result snippets),
            and per-step timing. Useful when you're debugging a weird answer or
            measuring latency. Adds ~1–3 KB to the response payload; OFF by default.
          </p>
        </div>
        <button
          onClick={toggle}
          disabled={enabled === null || saving}
          className={cn(
            "shrink-0 relative inline-flex h-6 w-11 items-center rounded-full transition",
            enabled ? "bg-violet-500" : "bg-muted",
            (enabled === null || saving) && "opacity-60 cursor-wait",
          )}
          aria-pressed={!!enabled}
        >
          <span
            className={cn(
              "inline-block h-4 w-4 transform rounded-full bg-white transition",
              enabled ? "translate-x-6" : "translate-x-1",
            )}
          />
        </button>
      </div>
    </Card>
  );
}

type DocVisibility = "private" | "business" | "shared";
const DEFAULT_DOC_VIS_OPTIONS: { value: DocVisibility; label: string; emoji: string; desc: string }[] = [
  { value: "private",  emoji: "🔒", label: "Private",  desc: "Only you + admin see new uploads." },
  { value: "business", emoji: "💼", label: "Business", desc: "Visible to the business group (employees, partners)." },
  { value: "shared",   emoji: "👥", label: "Shared",   desc: "Visible to the whole household / team." },
];

function DefaultDocVisibilityChips({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const [value, setValue] = useState<DocVisibility | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.get<{ logged_in: boolean; user?: any }>("/api/auth/me");
        setValue((r.user?.default_doc_visibility as DocVisibility) || "private");
      } catch {
        setValue("private");
      }
    })();
  }, []);

  async function pick(v: DocVisibility) {
    if (v === value) return;
    const prev = value;
    setValue(v);
    setSaving(true);
    try {
      await api.patch("/api/profile/default-doc-visibility", { visibility: v });
      toast(`Default upload visibility: ${v}`, "success");
    } catch (e: any) {
      setValue(prev);
      toast(e.message || "Failed to save", "error");
    } finally { setSaving(false); }
  }

  return (
    <Card title="Default visibility for new documents">
      <p className="text-xs text-muted-foreground mb-3">
        Every document you upload (via Yorik or auto-filed from email / WhatsApp) gets this
        visibility tag applied automatically. You can override per-document later.
      </p>
      <div className="flex flex-wrap gap-2">
        {DEFAULT_DOC_VIS_OPTIONS.map(opt => (
          <button
            key={opt.value}
            onClick={() => pick(opt.value)}
            disabled={value === null || saving}
            className={cn(
              "text-left p-3 rounded-lg border min-w-[180px] flex-1 transition",
              value === opt.value
                ? "bg-primary/10 border-primary/40"
                : "bg-card border-border hover:bg-muted/50",
              (value === null || saving) && "opacity-60",
            )}
          >
            <div className="text-sm font-medium flex items-center gap-1.5">
              <span>{opt.emoji}</span> {opt.label}
            </div>
            <div className="text-[11px] text-muted-foreground mt-0.5">{opt.desc}</div>
          </button>
        ))}
      </div>
    </Card>
  );
}

// ─── Signature upload (handwritten image) ───────────────────────────

function SignatureUpload({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const auth = useAuth();
  const [dataUrl, setDataUrl] = useState<string>(auth.user.signature_data_url || "");
  const [saving, setSaving] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // 200 KB cap — typical scanned-signature PNG is well under this; any
  // larger and the user probably scanned at the wrong resolution. SVG
  // signatures are usually <20 KB; we keep the same ceiling either way.
  const MAX_BYTES = 200 * 1024;
  const ACCEPTED_TYPES = new Set([
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
  ]);

  /** Strip script / event-handler / foreignObject content from SVG markup
   *  before storing. Templates render the signature via `<img src=...>`
   *  which already sandboxes script execution, but we sanitize defence-
   *  in-depth in case some future code path renders SVG inline. */
  function sanitizeSvg(raw: string): string {
    try {
      const doc = new DOMParser().parseFromString(raw, "image/svg+xml");
      const root = doc.documentElement;
      if (!root || root.nodeName === "parsererror") return raw;
      // Walk + scrub.
      const walker = doc.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
      const toRemove: Element[] = [];
      let n: Node | null = walker.currentNode;
      while (n) {
        const el = n as Element;
        const tag = el.tagName.toLowerCase();
        if (tag === "script" || tag === "foreignobject" || tag === "iframe") {
          toRemove.push(el);
        } else {
          // Remove on* event handlers and external/javascript: hrefs.
          for (const attr of Array.from(el.attributes)) {
            const name = attr.name.toLowerCase();
            const val = attr.value.trim().toLowerCase();
            if (name.startsWith("on")) {
              el.removeAttribute(attr.name);
            } else if ((name === "href" || name === "xlink:href")
                       && (val.startsWith("javascript:") || val.startsWith("vbscript:"))) {
              el.removeAttribute(attr.name);
            }
          }
        }
        n = walker.nextNode();
      }
      for (const el of toRemove) el.parentNode?.removeChild(el);
      return new XMLSerializer().serializeToString(doc);
    } catch {
      return raw;
    }
  }

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    if (!ACCEPTED_TYPES.has(f.type)) {
      toast("Pick a PNG, JPEG, GIF, WebP, or SVG image.", "error");
      return;
    }
    if (f.size > MAX_BYTES) {
      toast(`Signature too large (${Math.round(f.size / 1024)} KB). Max 200 KB — try scanning at lower resolution.`, "error");
      return;
    }

    setSaving(true);
    try {
      let url: string;
      if (f.type === "image/svg+xml") {
        // Sanitize SVG markup BEFORE base64-encoding so the stored data
        // URL is clean. Re-encode as UTF-8 base64.
        const raw = await f.text();
        const clean = sanitizeSvg(raw);
        const b64 = btoa(unescape(encodeURIComponent(clean)));
        url = `data:image/svg+xml;base64,${b64}`;
      } else {
        url = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result || ""));
          reader.onerror = () => reject(new Error("Couldn't read the file."));
          reader.readAsDataURL(f);
        });
      }
      await api.patch("/api/profile", { signature_data_url: url });
      setDataUrl(url);
      await auth.refresh();
      toast("Signature uploaded — it'll appear on new letters.", "success");
    } catch (err: any) {
      toast(`Save failed: ${err.message || err}`, "error");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!confirm("Remove your signature? Letters will fall back to a blank signature line.")) return;
    setSaving(true);
    try {
      await api.patch("/api/profile", { signature_data_url: null });
      setDataUrl("");
      await auth.refresh();
      toast("Signature removed.", "success");
    } catch (err: any) {
      toast(`Remove failed: ${err.message || err}`, "error");
    } finally { setSaving(false); }
  }

  return (
    <Card title="Signature">
      <p className="text-xs text-muted-foreground mb-3">
        Scan your handwritten signature once and Yorik puts it above your name on every letter
        you compose. PNG, JPEG, GIF, WebP or SVG — ≤200 KB. Transparent or white background works
        best. SVG (e.g. exported from an iPad drawing app) scales sharply at any size.
      </p>
      {dataUrl ? (
        <div className="flex items-start gap-4">
          <div className="border border-border rounded-md p-2 bg-white">
            <img
              src={dataUrl}
              alt="Your signature"
              style={{ maxWidth: 240, maxHeight: 80, display: "block" }}
            />
          </div>
          <div className="flex flex-col gap-2">
            <button
              onClick={() => fileRef.current?.click()}
              disabled={saving}
              className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted disabled:opacity-50"
            >
              Replace
            </button>
            <button
              onClick={remove}
              disabled={saving}
              className="text-xs px-3 py-1.5 rounded-md border border-border text-red-600 hover:bg-red-500/10 disabled:opacity-50"
            >
              Remove
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => fileRef.current?.click()}
          disabled={saving}
          className="text-xs px-4 py-2 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Upload className="w-3.5 h-3.5" />}
          Upload signature image
        </button>
      )}
      <input
        ref={fileRef}
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml"
        onChange={onFile}
        className="hidden"
      />
    </Card>
  );
}

// ─── Storage tab ─────────────────────────────────────────────────────

function StorageTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  return (
    <div className="space-y-4">
      <Card title="Where photos + documents live">
        <p className="text-xs text-muted-foreground mb-4 leading-relaxed">
          Yorik's databases stay on the internal disk (small, hot). Photos and document originals
          can be huge — relocate them to an external SSD to keep your laptop's main disk happy.
          The relocation is transparent: code keeps reading from <code className="font-mono">data/</code> via
          symlinks, so nothing else needs to change. Pull the SSD and Yorik refuses to start (loud, not silent).
        </p>
        {/* Imported lazily to keep the bundle from including it on tabs that don't need it. */}
        <StoragePicker />
      </Card>
    </div>
  );
}

// ─── Embeddings tab ──────────────────────────────────────────────────

interface WorkerSnapshot {
  name: string; status: string; detail: string; kind: string;
  last_heartbeat_age_s: number | null;
  uptime_s: number;
  error_count: number;
}

interface TaxonomyTagCount {
  id: string;
  label_de: string;
  label_en: string;
  category_id: string;
  count: number;
}

interface EmbeddingsStatus {
  vec_count:   number;
  chunk_count: number;
  doc_count:   number;
  embedder: {
    backend:        string;
    reachable:      boolean;
    external_url:   string | null;
    external_model: string;
    local_model:    string;
    dim:            number;
  };
  reconciler: WorkerSnapshot | null;
  autotagger: WorkerSnapshot | null;
  taxonomy_tag_counts: TaxonomyTagCount[];
}

function EmbeddingsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [status, setStatus] = useState<EmbeddingsStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [reindexing, setReindexing] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const s = await api.get<EmbeddingsStatus>("/api/embeddings/status");
      setStatus(s);
    } catch (e: any) {
      toast(`Failed to load embeddings status: ${e.message}`, "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    refresh();
    // Poll every 5s so the user watching a reindex job sees progress.
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  async function reindexAll() {
    if (!confirm(
      "Reindex every Paperless document?\n\n" +
      "This walks the whole corpus, re-extracts text, and re-embeds every chunk. " +
      "For ~2500 docs on a CPU embedder, expect 5–15 minutes. The job runs in the " +
      "background — you can close this page."
    )) return;
    setReindexing(true);
    try {
      await api.post("/api/paperless/reindex-all", {});
      toast("Reindex queued — watch the progress here.", "success");
      // Immediate refresh, then the 5s poll picks up the rest.
      refresh();
    } catch (e: any) {
      toast(`Reindex failed to queue: ${e.message}`, "error");
    } finally {
      setReindexing(false);
    }
  }

  const [autotagging, setAutotagging] = useState(false);
  const [stopping, setStopping] = useState(false);
  async function stopAutotagger() {
    setStopping(true);
    try {
      await api.post("/api/embeddings/autotag-cancel", {});
      toast("Stop requested — finishing current document, then exiting.", "info");
      refresh();
    } catch (e: any) {
      toast(`Stop request failed: ${e.message}`, "error");
    } finally {
      setStopping(false);
    }
  }
  async function autotagAll(force: boolean) {
    if (!confirm(
      (force
        ? "Re-run autotagger on EVERY document — including ones that already have tags?"
        : "Autotag every Paperless document?") + "\n\n" +
      "The LLM picks 0–3 tags per doc from a curated taxonomy (no invented tags, " +
      "no deletions). Existing user-added tags are kept. For ~2500 docs on a " +
      "local LLM, expect 30–90 minutes — runs in the background, watch progress here."
    )) return;
    setAutotagging(true);
    try {
      await api.post("/api/embeddings/autotag-all", { force_retag: force });
      toast(force ? "Autotagger queued (force re-run)" : "Autotagger queued", "success");
      refresh();
    } catch (e: any) {
      toast(`Autotagger failed to queue: ${e.message}`, "error");
    } finally {
      setAutotagging(false);
    }
  }

  if (!status && loading) {
    return (
      <div className="py-16 flex justify-center text-muted-foreground">
        <Loader2 className="w-5 h-5 animate-spin" />
      </div>
    );
  }
  if (!status) {
    return (
      <div className="py-16 text-center text-sm text-muted-foreground">
        Couldn't load embeddings status. <button onClick={refresh} className="underline">Retry</button>
      </div>
    );
  }

  const pct = status.chunk_count > 0
    ? Math.min(100, Math.round((status.vec_count / status.chunk_count) * 100))
    : 0;
  const backlog = Math.max(0, status.chunk_count - status.vec_count);
  const reachable = status.embedder.reachable;
  const recon = status.reconciler;

  // Autotagger is "running" when it's actively heartbeating and hasn't
  // emitted the DONE/STOPPED finishing line. Stale heartbeat (>60s) or
  // a terminal detail string means done — Stop button hides, Start
  // buttons re-enable.
  const at = status.autotagger;
  const atDetail = at?.detail || "";
  const atFresh = (at?.last_heartbeat_age_s ?? 999) < 60;
  const atTerminal = atDetail.startsWith("DONE") || atDetail.startsWith("STOPPED") || atDetail.startsWith("CANCELLED");
  const isAutotaggerRunning = !!at && !atTerminal && (at.status === "starting" || (at.status === "ok" && atFresh));

  return (
    <div>
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Embeddings</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Semantic search runs on a vector index of every Paperless chunk.
            This page shows the pipeline's health and lets you trigger a full reindex.
          </p>
        </div>
        <button
          onClick={refresh}
          className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted text-muted-foreground hover:text-foreground transition inline-flex items-center gap-1.5"
          title="Refresh"
        >
          <RefreshCw className={cn("w-3.5 h-3.5", loading && "animate-spin")} />
          Refresh
        </button>
      </header>

      <div className="space-y-4">

        {/* Index population */}
        <Card title="Index population">
          <div className="flex items-baseline justify-between mb-2">
            <div className="text-2xl font-semibold tabular-nums">
              {status.vec_count.toLocaleString()}
              <span className="text-muted-foreground text-base font-normal"> / {status.chunk_count.toLocaleString()} chunks embedded</span>
            </div>
            <div className="text-xs text-muted-foreground tabular-nums">
              {status.doc_count.toLocaleString()} documents · {pct}%
            </div>
          </div>
          <div className="h-2 rounded-full bg-muted overflow-hidden">
            <div
              className={cn(
                "h-full rounded-full transition-all",
                pct === 100 ? "bg-emerald-500" : "bg-violet-500",
              )}
              style={{ width: `${pct}%` }}
            />
          </div>
          {backlog > 0 && (
            <p className="text-xs text-muted-foreground mt-2">
              {backlog.toLocaleString()} chunks still to embed.
              {recon?.status === "ok" && " The reconciler is catching up automatically."}
              {!reachable && " Embedder unreachable — fix it (below) before the backlog will clear."}
            </p>
          )}
          {backlog === 0 && status.chunk_count > 0 && (
            <p className="text-xs text-emerald-600 mt-2 inline-flex items-center gap-1.5">
              <CheckCircle2 className="w-3.5 h-3.5" /> Fully embedded.
            </p>
          )}
          {status.chunk_count === 0 && (
            <p className="text-xs text-amber-600 mt-2 inline-flex items-center gap-1.5">
              <AlertCircle className="w-3.5 h-3.5" />
              No Paperless chunks ingested yet. Click "Reindex all" below to walk your corpus.
            </p>
          )}
        </Card>

        {/* Embedder status */}
        <Card title="Embedder">
          <div className="grid grid-cols-[120px_1fr] gap-y-2 text-sm">
            <div className="text-muted-foreground">Reachable</div>
            <div className="inline-flex items-center gap-1.5">
              {reachable ? (
                <><CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" /><span className="text-emerald-600 font-medium">Yes</span></>
              ) : (
                <><AlertCircle className="w-3.5 h-3.5 text-amber-500" /><span className="text-amber-600 font-medium">No</span></>
              )}
            </div>
            <div className="text-muted-foreground">Backend</div>
            <div className="font-mono text-xs">{status.embedder.backend}</div>
            <div className="text-muted-foreground">Local model</div>
            <div className="font-mono text-xs break-all">{status.embedder.local_model}</div>
            {status.embedder.external_url && (
              <>
                <div className="text-muted-foreground">External URL</div>
                <div className="font-mono text-xs break-all">{status.embedder.external_url}</div>
                <div className="text-muted-foreground">External model</div>
                <div className="font-mono text-xs">{status.embedder.external_model}</div>
              </>
            )}
            <div className="text-muted-foreground">Dimension</div>
            <div className="font-mono text-xs">{status.embedder.dim}</div>
          </div>
          {!reachable && (
            <div className="mt-3 p-3 rounded-md bg-amber-500/10 border border-amber-500/30 text-xs text-amber-700 dark:text-amber-300">
              <div className="font-medium mb-1 inline-flex items-center gap-1.5">
                <AlertCircle className="w-3.5 h-3.5" /> Embedder not reachable
              </div>
              <p className="leading-relaxed">
                On <code className="font-mono">auto</code>/<code className="font-mono">local</code> backend, this usually means
                <code className="font-mono"> sentence-transformers</code> isn't installed in the venv yet. Run{" "}
                <code className="font-mono">venv/bin/pip install -r backend/requirements.txt</code> and restart, then refresh this page.
                First call downloads the model (~120 MB, one-time).
              </p>
            </div>
          )}
        </Card>

        {/* Reconciler heartbeat */}
        <Card title="Background reconciler">
          {recon ? (
            <div className="grid grid-cols-[140px_1fr] gap-y-2 text-sm">
              <div className="text-muted-foreground">Status</div>
              <div className="inline-flex items-center gap-1.5">
                {recon.status === "ok" && <><CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" /><span className="text-emerald-600 font-medium">Healthy</span></>}
                {recon.status === "warn" && <><AlertCircle className="w-3.5 h-3.5 text-amber-500" /><span className="text-amber-600 font-medium">Warning</span></>}
                {recon.status === "error" && <><AlertCircle className="w-3.5 h-3.5 text-red-500" /><span className="text-red-600 font-medium">Error</span></>}
                {recon.status === "starting" && <><Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground" /><span className="text-muted-foreground">Starting</span></>}
              </div>
              <div className="text-muted-foreground">Last heartbeat</div>
              <div>{recon.last_heartbeat_age_s == null ? "never" : formatAge(recon.last_heartbeat_age_s)}</div>
              <div className="text-muted-foreground">Detail</div>
              <div className="text-xs">{recon.detail || <span className="text-muted-foreground">—</span>}</div>
              <div className="text-muted-foreground">Uptime</div>
              <div className="text-xs">{formatAge(recon.uptime_s)}</div>
              {recon.error_count > 0 && (
                <>
                  <div className="text-muted-foreground">Errors</div>
                  <div className="text-red-600 font-medium">{recon.error_count}</div>
                </>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              Reconciler hasn't reported yet. It registers itself on first run (boot + every 6h).
            </p>
          )}
          <p className="text-[11px] text-muted-foreground mt-3">
            Runs every 6 hours. Walks Paperless, ingests + embeds any new docs. Failures are surfaced here AND in /api/dashboard/workers.
          </p>
        </Card>

        {/* Manual reindex */}
        <Card title="Manual reindex">
          <p className="text-xs text-muted-foreground mb-4 leading-relaxed">
            Walk every Paperless document, re-extract text, and re-embed every chunk.
            Useful after changing the embedder, swapping models, or recovering from a corrupted index.
            For ~2500 docs on a CPU embedder this takes 5–15 minutes; the job runs in the background.
          </p>
          <button
            onClick={reindexAll}
            disabled={reindexing || !reachable}
            className={cn(
              "px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-2 transition",
              reindexing || !reachable
                ? "bg-muted text-muted-foreground cursor-not-allowed"
                : "bg-violet-500 hover:bg-violet-600 text-white",
            )}
          >
            {reindexing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Sparkles className="w-3.5 h-3.5" />}
            {reindexing ? "Queuing…" : "Reindex all Paperless documents"}
          </button>
          {!reachable && (
            <p className="text-[11px] text-amber-600 mt-2">Embedder needs to be reachable first.</p>
          )}
        </Card>

        {/* Autotagger */}
        <Card title="Autotagger">
          <p className="text-xs text-muted-foreground mb-3 leading-relaxed">
            The LLM picks 0–3 tags per document from Yorik's curated taxonomy
            (~66 tags, German + English, across categories like Finanzen, Versicherung,
            Wohnen, Gesundheit…). Tags appear in Paperless's UI <strong>and</strong> in
            the folder tree on the /documents page. Safety: only ever ADDs tags
            (never deletes), preserves any user-added tags, never invents new ones.
          </p>

          {status.autotagger ? (
            <div className="mb-3 p-3 rounded-md bg-card border border-border text-xs space-y-1">
              <div className="flex items-center gap-1.5">
                {status.autotagger.status === "ok" && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" />}
                {status.autotagger.status === "warn" && <AlertCircle className="w-3.5 h-3.5 text-amber-500" />}
                {status.autotagger.status === "error" && <AlertCircle className="w-3.5 h-3.5 text-red-500" />}
                {status.autotagger.status === "starting" && <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground" />}
                <span className="font-medium">{status.autotagger.status}</span>
                <span className="text-muted-foreground">·</span>
                <span className="text-muted-foreground">
                  {status.autotagger.last_heartbeat_age_s == null ? "never" : formatAge(status.autotagger.last_heartbeat_age_s)}
                </span>
              </div>
              {status.autotagger.detail && (
                <div className="font-mono text-[11px] text-foreground/80">{status.autotagger.detail}</div>
              )}
            </div>
          ) : (
            <p className="text-[11px] text-muted-foreground mb-3 italic">
              Autotagger hasn't run yet. Click below to start the first pass.
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => autotagAll(false)}
              disabled={autotagging || isAutotaggerRunning}
              className={cn(
                "px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-2 transition",
                (autotagging || isAutotaggerRunning)
                  ? "bg-muted text-muted-foreground cursor-not-allowed"
                  : "bg-amber-500 hover:bg-amber-600 text-white",
              )}
            >
              {autotagging ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <TagIcon className="w-3.5 h-3.5" />}
              {autotagging ? "Queuing…" : "Autotag untagged docs"}
            </button>
            <button
              onClick={() => autotagAll(true)}
              disabled={autotagging || isAutotaggerRunning}
              className={cn(
                "px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-2 transition border",
                (autotagging || isAutotaggerRunning)
                  ? "bg-muted text-muted-foreground border-border cursor-not-allowed"
                  : "border-amber-500/40 text-amber-700 dark:text-amber-300 hover:bg-amber-500/10",
              )}
            >
              <RefreshCw className="w-3.5 h-3.5" />
              Re-tag everything
            </button>
            {isAutotaggerRunning && (
              <button
                onClick={stopAutotagger}
                disabled={stopping}
                className={cn(
                  "px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-2 transition border",
                  stopping
                    ? "bg-muted text-muted-foreground border-border cursor-not-allowed"
                    : "border-red-500/40 text-red-600 hover:bg-red-500/10",
                )}
              >
                {stopping ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <XCircle className="w-3.5 h-3.5" />}
                {stopping ? "Stopping…" : "Stop"}
              </button>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground mt-2">
            ~30–90 min for 2500 docs on a local LLM. First button skips docs that already
            carry a taxonomy tag; second forces a full re-run.
          </p>
          <p className="text-[11px] text-muted-foreground mt-1 inline-flex items-start gap-1.5">
            <Info className="w-3 h-3 mt-0.5 shrink-0 opacity-70" />
            <span>
              The job runs on the server, not in your browser — safe to close this page or shut down your laptop's lid.
              Progress and counts below survive reloads. Stop is cooperative: the current document finishes (an in-flight
              LLM call), then the walk exits cleanly.
            </span>
          </p>
        </Card>

        {/* Tag distribution */}
        {status.taxonomy_tag_counts.length > 0 && (
          <Card title="Tag distribution">
            <p className="text-xs text-muted-foreground mb-3 leading-relaxed">
              Counts across all docs in Paperless for the {status.taxonomy_tag_counts.length} taxonomy tag(s) currently in use.
              User-added tags outside the taxonomy aren't listed here.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {status.taxonomy_tag_counts.slice(0, 80).map(tc => (
                <span
                  key={tc.id}
                  title={`${tc.label_de} / ${tc.label_en} · category: ${tc.category_id}`}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-medium bg-amber-500/10 text-amber-700 dark:text-amber-300 border border-amber-500/20"
                >
                  <span>{tc.label_de}</span>
                  <span className="opacity-60">·</span>
                  <span className="tabular-nums">{tc.count}</span>
                </span>
              ))}
            </div>
            {status.taxonomy_tag_counts.length > 80 && (
              <p className="text-[11px] text-muted-foreground mt-2">
                +{status.taxonomy_tag_counts.length - 80} more not shown.
              </p>
            )}
          </Card>
        )}

      </div>
    </div>
  );
}

function formatAge(seconds: number): string {
  if (seconds < 60)   return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}


// ─── Backup tab ──────────────────────────────────────────────────────

function BackupTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  return (
    <div className="space-y-4">
      <Card title="Encrypted backups">
        <p className="text-xs text-muted-foreground mb-4 leading-relaxed">
          Snapshots are tar+gzip+age-encrypted with a passphrase only you know.
          Calendars, contacts, tasks and the encryption key are always included.
          Photos, Paperless and the WhatsApp session are opt-in (they're heavy).
        </p>
        <BackupPicker />
      </Card>
    </div>
  );
}

// ─── Numbering tab (mounts the existing SeriesManager modal as inline view) ──

// ─── LLM tab ──────────────────────────────────────────────────────────

interface LlmConfig {
  base_url: string;
  model: string;
  reachable: boolean;
  reason?: string;
  served_models: string[];
  has_api_key: boolean;
}

interface LlmCandidate {
  label: string;
  base_url: string;
  ok: boolean;
  reason?: string;
  models: string[];
}

function LlmTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [cfg, setCfg] = useState<LlmConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [draftUrl, setDraftUrl] = useState("");
  const [draftModel, setDraftModel] = useState("");
  // draftApiKey is null = "no change", "" = "clear stored key", "abc..." = "set to this".
  // Never pre-populated from the server (the GET endpoint only returns has_api_key).
  const [draftApiKey, setDraftApiKey] = useState<string | null>(null);
  const [showApiKey, setShowApiKey] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probeResult, setProbeResult] = useState<{ ok: boolean; models: string[]; reason?: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [candidates, setCandidates] = useState<LlmCandidate[] | null>(null);
  const [detecting, setDetecting] = useState(false);
  // Flip true on the save that takes the endpoint from unreachable
  // (or unset) to reachable. Drives the post-connection guidance
  // banner — first-time configs get a clear "now ask the chat
  // 'what should I do next?'" nudge.
  const [justConnected, setJustConnected] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const c = await api.get<LlmConfig>("/api/llm/config");
      setCfg(c);
      setDraftUrl(c.base_url);
      setDraftModel(c.model);
      setProbeResult(c.reachable
        ? { ok: true, models: c.served_models }
        : { ok: false, models: [], reason: c.reason });
    } catch (e: any) {
      toast(`Couldn't load LLM config: ${e.message}`, "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { refresh(); }, [refresh]);

  async function testEndpoint(url: string) {
    setProbing(true);
    try {
      // Send the draft api_key if the user typed one but hasn't saved yet —
      // otherwise probe uses whatever's stored. null/undefined = "use stored".
      const payload: { base_url: string; api_key?: string } = { base_url: url };
      if (draftApiKey !== null) payload.api_key = draftApiKey;
      const r = await api.post<{ ok: boolean; models: string[]; reason?: string }>(
        "/api/llm/probe", payload,
      );
      setProbeResult(r);
      if (r.ok && r.models.length > 0 && !r.models.includes(draftModel)) {
        // Pick the first served model as a sensible default
        setDraftModel(r.models[0]);
      }
    } catch (e: any) {
      setProbeResult({ ok: false, models: [], reason: e.message });
    } finally {
      setProbing(false);
    }
  }

  async function detect() {
    setDetecting(true);
    try {
      const r = await api.get<{ candidates: LlmCandidate[] }>("/api/llm/detect");
      setCandidates(r.candidates);
    } catch (e: any) {
      toast(`Detection failed: ${e.message}`, "error");
    } finally {
      setDetecting(false);
    }
  }

  async function save() {
    if (!draftUrl.trim() || !draftModel.trim()) {
      toast("URL and model are both required", "error");
      return;
    }
    setSaving(true);
    try {
      const body: { base_url: string; model: string; api_key?: string } = {
        base_url: draftUrl.trim(),
        model:    draftModel.trim(),
      };
      // null = "leave stored key alone". "" = "clear it". A real string = "set to this".
      if (draftApiKey !== null) body.api_key = draftApiKey;
      const wasReachable = cfg?.reachable === true;
      const c = await api.patch<LlmConfig>("/api/llm/config", body);
      setCfg(c);
      // Reset the draft key — the actual value is now stored server-side and
      // never re-displayed. Re-edit means typing it again.
      setDraftApiKey(null);
      setShowApiKey(false);
      // First-time-connected case → surface the guidance banner.
      if (c.reachable && !wasReachable) setJustConnected(true);
      toast("LLM endpoint saved — next chat turn uses it", "success");
    } catch (e: any) {
      toast(e.message || "Save failed", "error");
    } finally {
      setSaving(false);
    }
  }

  function useCandidate(c: LlmCandidate, model: string) {
    setDraftUrl(c.base_url);
    setDraftModel(model);
    setProbeResult({ ok: true, models: c.models });
  }

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">LLM endpoint</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Yorik talks to any OpenAI-compatible local LLM — Ollama, LM
          Studio, llama.cpp, vLLM. Changes take effect on the next chat
          turn without a restart.
        </p>
        <p className="text-xs text-muted-foreground mt-3">
          <strong>Don't have one yet?</strong> Fastest path is Ollama:
        </p>
        <pre className="text-[11px] bg-muted/40 border border-border rounded-md px-3 py-2 mt-1 font-mono whitespace-pre overflow-x-auto">{`curl -fsSL https://ollama.com/install.sh | sh
ollama serve &`}</pre>
        <p className="text-xs text-muted-foreground mt-2">
          Then <code className="font-mono text-[11px]">ollama pull</code> a
          tool-calling chat model (Yorik is tested with Qwen 3.5 9B,
          standard and MTP variants), click <strong>Scan now</strong> below,
          and pick the endpoint.
        </p>
      </header>

      {justConnected && cfg?.reachable && (
        <div className="mb-6 rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-4 py-3">
          <div className="flex items-start gap-3">
            <CheckCircle2 className="w-5 h-5 text-emerald-600 mt-0.5 shrink-0" />
            <div className="flex-1">
              <div className="text-sm font-semibold text-emerald-700 dark:text-emerald-300">
                LLM connected.
              </div>
              <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
                Open the chat and ask <em>"what should I do next?"</em> — Yorik will walk you through
                connecting an email account, importing your documents, and setting up photo backup.
              </p>
              <div className="mt-3 flex items-center gap-2 flex-wrap">
                <a
                  href="/r/chat"
                  className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-700 dark:text-emerald-300 transition"
                >
                  Open chat
                </a>
                <button
                  onClick={() => setJustConnected(false)}
                  className="text-[11px] text-muted-foreground hover:text-foreground transition"
                >
                  Dismiss
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {loading && !cfg && (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
        </div>
      )}

      {cfg && (
        <div className="space-y-5">
          <Card title="Current">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-xs text-muted-foreground">Endpoint</div>
                <div className="font-mono text-sm truncate">{cfg.base_url}</div>
                <div className="text-xs text-muted-foreground mt-2">Model</div>
                <div className="font-mono text-sm truncate">{cfg.model}</div>
              </div>
              <div className="shrink-0 text-right">
                {cfg.reachable
                  ? <span className="inline-flex items-center gap-1 text-emerald-600 text-xs"><CheckCircle2 className="w-3.5 h-3.5" /> reachable</span>
                  : <span className="inline-flex items-center gap-1 text-red-500 text-xs"><AlertCircle className="w-3.5 h-3.5" /> offline</span>}
                {!cfg.reachable && cfg.reason && (
                  <div className="text-[10px] text-muted-foreground mt-0.5">{cfg.reason}</div>
                )}
                <button
                  onClick={refresh}
                  className="text-[10px] text-muted-foreground hover:text-foreground transition mt-1 inline-flex items-center gap-1"
                >
                  <RefreshCw className="w-2.5 h-2.5" /> Re-check
                </button>
              </div>
            </div>
          </Card>

          <Card title="Detect local endpoints">
            <p className="text-xs text-muted-foreground mb-3">
              Quick scan of common ports — Ollama (11434), llama-swap (8080),
              LM Studio (1234), vLLM (8001), llama.cpp (8081). Click a found
              endpoint to use it.
            </p>
            <button
              onClick={detect}
              disabled={detecting}
              className={cn(
                "inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-md transition",
                "bg-blue-500 hover:bg-blue-600 text-white shadow-sm",
                detecting && "opacity-60 cursor-wait",
              )}
            >
              {detecting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Search className="w-3.5 h-3.5" />}
              Scan now
            </button>
            {candidates && (
              <div className="mt-3 space-y-1.5">
                {candidates.map(c => (
                  <div key={c.base_url} className={cn(
                    "border rounded-md px-3 py-2 text-xs",
                    c.ok ? "bg-emerald-500/5 border-emerald-500/20" : "bg-muted/30 border-border",
                  )}>
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        {c.ok
                          ? <Wifi className="w-3.5 h-3.5 text-emerald-600 shrink-0" />
                          : <AlertCircle className="w-3.5 h-3.5 text-muted-foreground shrink-0" />}
                        <span className="font-medium">{c.label}</span>
                        <span className="text-muted-foreground font-mono truncate">{c.base_url}</span>
                      </div>
                      {c.ok
                        ? <span className="text-[10px] text-emerald-600">{c.models.length} model{c.models.length === 1 ? "" : "s"}</span>
                        : <span className="text-[10px] text-muted-foreground">{c.reason}</span>}
                    </div>
                    {c.ok && c.models.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-2">
                        {c.models.map(m => (
                          <button
                            key={m}
                            onClick={() => useCandidate(c, m)}
                            className="text-[10px] px-1.5 py-0.5 rounded bg-card border border-border hover:border-blue-500/40 font-mono"
                            title={`Use ${m} via ${c.label}`}
                          >
                            {m}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card title="Change endpoint">
            <Field label="Endpoint URL (must end with /v1)">
              <div className="flex gap-2">
                <input
                  value={draftUrl}
                  onChange={e => { setDraftUrl(e.target.value); setProbeResult(null); }}
                  placeholder="http://127.0.0.1:11434/v1"
                  className={cn(inputClass, "font-mono flex-1")}
                />
                <button
                  onClick={() => testEndpoint(draftUrl)}
                  disabled={probing || !draftUrl.trim()}
                  className={cn(
                    "px-3 py-1.5 text-xs rounded-md font-medium transition inline-flex items-center gap-1.5 shrink-0",
                    "bg-muted hover:bg-muted/70 text-foreground disabled:opacity-50",
                  )}
                >
                  {probing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plug className="w-3.5 h-3.5" />}
                  Test
                </button>
              </div>
            </Field>

            {probeResult && (
              <div className={cn(
                "mt-3 text-xs rounded-md px-3 py-2 flex items-start gap-2",
                probeResult.ok
                  ? "bg-emerald-500/10 border border-emerald-500/20 text-emerald-700 dark:text-emerald-400"
                  : "bg-red-500/10 border border-red-500/20 text-red-600",
              )}>
                {probeResult.ok
                  ? <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                  : <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />}
                <span className="flex-1 min-w-0">
                  {probeResult.ok
                    ? `Reachable. ${probeResult.models.length} model(s) served.`
                    : `Can't reach: ${probeResult.reason}`}
                </span>
              </div>
            )}

            <div className="mt-4">
              <Field label="Model">
                {probeResult?.ok && probeResult.models.length > 0 ? (
                  <select
                    value={draftModel}
                    onChange={e => setDraftModel(e.target.value)}
                    className={cn(inputClass, "font-mono")}
                  >
                    {probeResult.models.map(m => (
                      <option key={m} value={m}>{m}</option>
                    ))}
                    {!probeResult.models.includes(draftModel) && draftModel && (
                      <option value={draftModel}>{draftModel} (not in current list)</option>
                    )}
                  </select>
                ) : (
                  <input
                    value={draftModel}
                    onChange={e => setDraftModel(e.target.value)}
                    placeholder="model-name (e.g. from `ollama list`)"
                    className={cn(inputClass, "font-mono")}
                  />
                )}
              </Field>
              <div className="text-[10px] text-muted-foreground mt-1">
                Test the endpoint first to populate the dropdown from its
                served-model list. Or type a model id directly if you know it.
              </div>
            </div>

            <div className="mt-4">
              <Field label="API key (only for cloud / authenticated endpoints)">
                <div className="flex gap-2">
                  <input
                    type={showApiKey ? "text" : "password"}
                    value={draftApiKey ?? ""}
                    onChange={e => setDraftApiKey(e.target.value)}
                    placeholder={cfg.has_api_key ? "•••••••• (stored — leave blank to keep)" : "sk-… or leave blank for local endpoints"}
                    className={cn(inputClass, "font-mono flex-1")}
                    autoComplete="off"
                  />
                  <button
                    type="button"
                    onClick={() => setShowApiKey(s => !s)}
                    className={cn(
                      "px-3 py-1.5 text-xs rounded-md font-medium transition shrink-0",
                      "bg-muted hover:bg-muted/70 text-foreground",
                    )}
                  >
                    {showApiKey ? "Hide" : "Show"}
                  </button>
                  {cfg.has_api_key && (
                    <button
                      type="button"
                      onClick={() => setDraftApiKey("")}
                      title="Clear the stored key on save"
                      className={cn(
                        "px-3 py-1.5 text-xs rounded-md font-medium transition shrink-0",
                        "bg-red-500/10 hover:bg-red-500/20 text-red-600 border border-red-500/20",
                      )}
                    >
                      Clear
                    </button>
                  )}
                </div>
              </Field>
              <div className="text-[10px] text-muted-foreground mt-1">
                Local servers (Ollama, llama-swap, LM Studio, vLLM) don't
                need a key. Required for OpenAI / Anthropic / similar cloud
                endpoints — prompts and chat content WILL leave the machine
                if you point Yorik at one. Stored encrypted with Fernet,
                never written to logs or config.env.
              </div>
            </div>

            {(() => {
              const draftMatchesProbe = probeResult?.ok
                && probeResult.models.includes(draftModel)
                && draftUrl === (probeResult as any)._probedUrl;
              const probeOkForDraft = probeResult?.ok
                && probeResult.models.includes(draftModel);
              const probeFailed = probeResult && !probeResult.ok;
              const unchanged = draftUrl === cfg.base_url && draftModel === cfg.model && draftApiKey === null;
              const saveDisabled = saving || !draftUrl.trim() || !draftModel.trim()
                                   || unchanged || !probeOkForDraft;
              return (
                <>
                  {!probeResult && !unchanged && (
                    <div className="mt-4 text-xs text-amber-600 dark:text-amber-400 flex items-start gap-2">
                      <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                      <span>Run <strong>Test</strong> first — Save is gated on a successful probe so a bad URL or model can't silently break chat.</span>
                    </div>
                  )}
                  {probeFailed && !unchanged && (
                    <div className="mt-4 text-xs text-red-600 dark:text-red-400 flex items-start gap-2">
                      <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                      <span>The endpoint didn't respond. Fix the URL/key or revert before saving — Save stays disabled until the probe succeeds.</span>
                    </div>
                  )}
                  {probeResult?.ok && !probeResult.models.includes(draftModel) && !unchanged && (
                    <div className="mt-4 text-xs text-amber-600 dark:text-amber-400 flex items-start gap-2">
                      <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                      <span>Endpoint is reachable but doesn't serve <code className="font-mono">{draftModel}</code>. Pick one from the served-model list.</span>
                    </div>
                  )}
                  <div className="flex justify-end mt-4">
                    <button
                      onClick={save}
                      disabled={saveDisabled}
                      className={cn(
                        "px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-1.5 transition",
                        "bg-gradient-to-r from-blue-500 to-cyan-500 hover:from-blue-600 hover:to-cyan-600 text-white shadow-md",
                        "disabled:opacity-60 disabled:cursor-not-allowed",
                      )}
                    >
                      {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                      Save + apply
                    </button>
                  </div>
                </>
              );
            })()}
          </Card>

          <STTConfigCard toast={toast} />

          <div className="text-[11px] text-muted-foreground leading-relaxed">
            <strong className="text-foreground/70">Tips:</strong> Ollama serves at <code>http://127.0.0.1:11434/v1</code>.
            LM Studio at <code>http://127.0.0.1:1234/v1</code>. vLLM is whatever <code>--port</code> you started it with.
            Whichever backend, it needs to expose an OpenAI-compatible
            <code>/v1/chat/completions</code> + <code>/v1/models</code>.
          </div>
        </div>
      )}
    </div>
  );
}


function NumberingTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Document numbering</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Sequential numbers for invoices, quotes, and other legal documents — with
          a tax-audit-proof audit trail. Required by law in Germany and Poland,
          best-practice everywhere else.
        </p>
      </header>
      <SeriesManager
        inline
        onClose={() => {}}
        onChanged={() => {}}
        toast={toast}
      />
    </div>
  );
}

// ─── Skills tab ───────────────────────────────────────────────────────
// Admin-only inventory + on/off control. Skills are grouped by a
// UI-only naming-convention category that the backend derives
// (`ui_category` on the /api/skills response) — NOT the legacy
// `category` field the LLM sees in {skill_index}. The LLM's view
// stays as it always was; this accordion gives the admin a
// navigable surface for managing 50+ skills + the ability to
// disable individual skills or whole categories (e.g. "we don't
// use WhatsApp on this install"). Disabled skills disappear from
// the LLM's next-turn skill_index and are refused at invoke time.
//
// Why no install/uninstall here: skill code is on disk, sandboxing
// for community-submitted code is post-beta. Toggling enabled
// state is the safe middle — admin curates the available surface
// without running unverified code.

interface SkillManifest {
  name: string;
  description: string;
  when_to_use?: string;
  inputs?: Record<string, { type?: string; required?: boolean; description?: string }>;
  permissions?: string[];
  side_effects?: string;
  tags?: string[];
  ui_category?: string;  // Settings-UI bucket; "System" for catch-all.
  disabled?: boolean;
}

interface SkillStat {
  total: number;
  successes: number;
  failures: number;
  last_used: string | null;
}

function SkillsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [skills, setSkills] = useState<SkillManifest[] | null>(null);
  const [stats, setStats]   = useState<Record<string, SkillStat>>({});
  const [expandedSkill, setExpandedSkill] = useState<string | null>(null);
  const [collapsedCats, setCollapsedCats] = useState<Set<string>>(new Set());
  // Track in-flight toggles so the UI can disable the affected control.
  const [busy, setBusy] = useState<Set<string>>(new Set());

  const refresh = useCallback(async () => {
    try {
      const [s, st] = await Promise.all([
        api.get<SkillManifest[]>("/api/skills"),
        api.get<Record<string, SkillStat>>("/api/skills/stats"),
      ]);
      setSkills(s);
      setStats(st || {});
    } catch (e: any) {
      toast(`Couldn't load skills: ${e.message}`, "error");
    }
  }, [toast]);

  useEffect(() => { refresh(); }, [refresh]);

  async function toggleSkill(name: string, nextEnabled: boolean) {
    setBusy(b => { const n = new Set(b); n.add(name); return n; });
    try {
      await api.patch(`/api/skills/${encodeURIComponent(name)}`, { enabled: nextEnabled });
      setSkills(curr => curr?.map(s => s.name === name ? { ...s, disabled: !nextEnabled } : s) || null);
    } catch (e: any) {
      toast(`Couldn't ${nextEnabled ? "enable" : "disable"} ${name}: ${e.message}`, "error");
    } finally {
      setBusy(b => { const n = new Set(b); n.delete(name); return n; });
    }
  }

  async function toggleCategory(cat: string, nextEnabled: boolean) {
    const key = `cat:${cat}`;
    setBusy(b => { const n = new Set(b); n.add(key); return n; });
    try {
      const r = await api.patch<{ flipped: string[] }>(
        `/api/skills/categories/${encodeURIComponent(cat)}`, { enabled: nextEnabled },
      );
      const flipped = new Set(r.flipped || []);
      setSkills(curr => curr?.map(s =>
        flipped.has(s.name) ? { ...s, disabled: !nextEnabled } : s,
      ) || null);
      if (r.flipped?.length) {
        toast(`${cat}: ${r.flipped.length} skill${r.flipped.length === 1 ? "" : "s"} ${nextEnabled ? "enabled" : "disabled"}`, "success");
      }
    } catch (e: any) {
      toast(`Couldn't toggle ${cat}: ${e.message}`, "error");
    } finally {
      setBusy(b => { const n = new Set(b); n.delete(key); return n; });
    }
  }

  if (!skills) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        <Loader2 className="w-5 h-5 animate-spin" />
      </div>
    );
  }

  // Group by ui_category. Order: predefined domain buckets first
  // (so Calendar/Tasks/Bills/etc. lead the page), then anything
  // unexpected, then System last.
  const CATEGORY_ORDER = [
    "Calendar", "Tasks", "Bills", "Contacts", "Documents",
    "Photos", "Email", "WhatsApp", "Compose", "System",
  ];
  const byCategory = new Map<string, SkillManifest[]>();
  for (const s of skills) {
    const cat = s.ui_category || "System";
    if (!byCategory.has(cat)) byCategory.set(cat, []);
    byCategory.get(cat)!.push(s);
  }
  const orderedCategories = [
    ...CATEGORY_ORDER.filter(c => byCategory.has(c)),
    ...[...byCategory.keys()].filter(c => !CATEGORY_ORDER.includes(c)).sort(),
  ];
  // Within a category: most-used first, then alphabetical.
  for (const cat of byCategory.keys()) {
    byCategory.get(cat)!.sort((a, b) => {
      const at = stats[a.name]?.total || 0;
      const bt = stats[b.name]?.total || 0;
      if (at !== bt) return bt - at;
      return a.name.localeCompare(b.name);
    });
  }

  const totalEnabled = skills.filter(s => !s.disabled).length;

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Skills</h1>
        <p className="text-sm text-muted-foreground mt-1">
          What Yorik can do. {totalEnabled} of {skills.length} skill{skills.length === 1 ? "" : "s"} enabled,
          grouped by category. Toggle whole categories from the header, or individual skills from each row.
          Disabled skills disappear from the LLM's next answer.
        </p>
      </header>

      <Card title="Got an idea for a new skill?">
        <p className="text-xs text-muted-foreground mb-2">
          A skill is two files in <code className="font-mono text-[11px]">backend/skills/</code>.
          ~50 lines. Scaffold one with{" "}
          <code className="font-mono text-[11px]">bash scripts/new-skill.sh &lt;name&gt;</code>{" "}
          then edit the two files. After backend restart, the LLM picks it up automatically.
        </p>
        <a
          href="https://github.com/winidi/yorik-ai/blob/main/docs/SKILLS.md"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-violet-500 hover:bg-violet-600 text-white font-medium transition"
        >
          <Lightbulb className="w-3.5 h-3.5" />
          Read the guide
          <ExternalLink className="w-3 h-3" />
        </a>
      </Card>

      <div className="mt-5 space-y-3">
        {orderedCategories.map(cat => {
          const members = byCategory.get(cat)!;
          const enabledCount = members.filter(s => !s.disabled).length;
          const catEnabled = enabledCount > 0;
          const catFullyEnabled = enabledCount === members.length;
          const catCollapsed = collapsedCats.has(cat);
          const catBusy = busy.has(`cat:${cat}`);

          return (
            <section key={cat} className="border border-border rounded-lg bg-card overflow-hidden">
              <div className="flex items-center gap-3 px-3 py-2.5 bg-muted/20 border-b border-border">
                <button
                  onClick={() => setCollapsedCats(c => {
                    const n = new Set(c);
                    if (n.has(cat)) n.delete(cat); else n.add(cat);
                    return n;
                  })}
                  className="flex items-center gap-2 flex-1 min-w-0 text-left"
                >
                  <ArrowDownIcon open={!catCollapsed} />
                  <span className="font-semibold text-sm">{cat}</span>
                  <span className="text-[10px] tabular-nums text-muted-foreground">
                    {enabledCount}/{members.length}
                  </span>
                  {!catFullyEnabled && catEnabled && (
                    <span className="text-[9px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-600 uppercase tracking-wider">
                      partial
                    </span>
                  )}
                </button>
                <button
                  onClick={() => toggleCategory(cat, !catEnabled)}
                  disabled={catBusy}
                  className={cn(
                    "shrink-0 relative inline-flex h-5 w-9 items-center rounded-full transition",
                    catEnabled ? "bg-violet-500" : "bg-muted",
                    catBusy && "opacity-60 cursor-wait",
                  )}
                  aria-pressed={catEnabled}
                  title={catEnabled ? `Disable all ${cat} skills` : `Enable all ${cat} skills`}
                >
                  <span className={cn(
                    "inline-block h-3.5 w-3.5 transform rounded-full bg-white transition",
                    catEnabled ? "translate-x-5" : "translate-x-1",
                  )} />
                </button>
              </div>

              {!catCollapsed && (
                <div className="divide-y divide-border">
                  {members.map(skill => {
                    const stat = stats[skill.name];
                    const isOpen = expandedSkill === skill.name;
                    const skillBusy = busy.has(skill.name);
                    const skillEnabled = !skill.disabled;
                    const successRate = stat && (stat.successes + stat.failures) > 0
                      ? Math.round((stat.successes / (stat.successes + stat.failures)) * 100)
                      : null;
                    return (
                      <div key={skill.name} className={cn("transition", !skillEnabled && "opacity-50")}>
                        <div className="flex items-center gap-3 px-3 py-2">
                          <button
                            onClick={() => setExpandedSkill(isOpen ? null : skill.name)}
                            className="flex-1 min-w-0 text-left"
                          >
                            <div className="flex items-center justify-between gap-3">
                              <div className="flex items-center gap-2 min-w-0">
                                <span className="font-mono text-sm">{skill.name}</span>
                                {skill.side_effects && skill.side_effects !== "none" && (
                                  <span className="text-[9px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-600 uppercase tracking-wider">
                                    mutates
                                  </span>
                                )}
                                {skill.tags?.includes("free") && (
                                  <span className="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-600 uppercase tracking-wider">
                                    free
                                  </span>
                                )}
                              </div>
                              <div className="flex items-center gap-3 text-[10px] tabular-nums text-muted-foreground shrink-0">
                                {stat ? (
                                  <>
                                    <span>{stat.total} call{stat.total === 1 ? "" : "s"}</span>
                                    {successRate !== null && (
                                      <span className={cn(
                                        "font-medium",
                                        successRate >= 80 ? "text-emerald-600" :
                                        successRate >= 50 ? "text-amber-600" : "text-red-500",
                                      )}>{successRate}% ok</span>
                                    )}
                                  </>
                                ) : (
                                  <span className="opacity-60">never called</span>
                                )}
                                <ArrowDownIcon open={isOpen} />
                              </div>
                            </div>
                            <p className="text-xs text-muted-foreground mt-1 line-clamp-2 pr-6">
                              {skill.description}
                            </p>
                          </button>
                          <button
                            onClick={() => toggleSkill(skill.name, !skillEnabled)}
                            disabled={skillBusy}
                            className={cn(
                              "shrink-0 relative inline-flex h-5 w-9 items-center rounded-full transition",
                              skillEnabled ? "bg-violet-500" : "bg-muted",
                              skillBusy && "opacity-60 cursor-wait",
                            )}
                            aria-pressed={skillEnabled}
                            title={skillEnabled ? `Disable ${skill.name}` : `Enable ${skill.name}`}
                          >
                            <span className={cn(
                              "inline-block h-3.5 w-3.5 transform rounded-full bg-white transition",
                              skillEnabled ? "translate-x-5" : "translate-x-1",
                            )} />
                          </button>
                        </div>
                        {isOpen && (
                          <div className="px-3 pb-3 pt-1 border-t border-border bg-muted/10 text-xs space-y-2.5">
                            {skill.when_to_use && (
                              <div>
                                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-0.5">When to use</div>
                                <div className="whitespace-pre-wrap text-[11px]">{skill.when_to_use.trim()}</div>
                              </div>
                            )}
                            {skill.inputs && Object.keys(skill.inputs).length > 0 && (
                              <div>
                                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-0.5">Inputs</div>
                                <div className="space-y-0.5">
                                  {Object.entries(skill.inputs).map(([key, spec]) => (
                                    <div key={key} className="text-[11px]">
                                      <span className="font-mono">{key}</span>
                                      {spec.required && <span className="text-red-500"> *</span>}
                                      {spec.type && <span className="text-muted-foreground"> · {spec.type}</span>}
                                      {spec.description && (
                                        <div className="ml-3 text-muted-foreground line-clamp-2">{spec.description}</div>
                                      )}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}
                            {skill.permissions && (
                              <div className="text-[11px]">
                                <span className="text-muted-foreground">Allowed roles: </span>
                                <span className="font-mono">{skill.permissions.join(", ")}</span>
                              </div>
                            )}
                            {stat?.last_used && (
                              <div className="text-[10px] text-muted-foreground">
                                Last used: {new Date(stat.last_used).toLocaleString()}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}

function ArrowDownIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 16 16"
      fill="currentColor"
      className={cn("transition-transform shrink-0", open && "rotate-180")}
      aria-hidden
    >
      <path d="M4 6l4 4 4-4z" />
    </svg>
  );
}


// ─── Quality tab ──────────────────────────────────────────────────────

interface QualitySummary {
  window_days: number;
  current_model: string;
  skills: Array<{ skill_id: string; llm_model: string | null; n: number; successes: number; avg_latency_ms: number | null }>;
  turns_by_model: Array<{ llm_model: string | null; n: number; up: number; down: number }>;
  templates: Array<{ template_id: string; llm_model: string | null; n: number; up: number; down: number }>;
  recent_failures: Array<{ skill_id: string; llm_model: string | null; error: string; created_at: string }>;
}

function QualityTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [data, setData] = useState<QualitySummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(30);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.get<QualitySummary>(`/api/quality/summary?role=admin&days=${days}`);
      setData(r);
    } catch (e: any) {
      toast(`Could not load quality: ${e.message}`, "error");
    } finally {
      setLoading(false);
    }
  }, [days, toast]);

  useEffect(() => { refresh(); }, [refresh]);

  return (
    <div>
      <header className="mb-6 flex items-baseline justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Quality dashboard</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Per-LLM success rates for skills, chat turns, and templates. The
            same data the community marketplace will rank by — when you opt in.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={days}
            onChange={e => setDays(Number(e.target.value))}
            className={cn(inputClass, "h-8 text-xs w-auto")}
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
            <option value={365}>Last year</option>
          </select>
          <button
            onClick={refresh}
            className="w-8 h-8 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center justify-center"
            title="Reload"
          >
            <RefreshCw className={cn("w-3.5 h-3.5", loading && "animate-spin")} />
          </button>
        </div>
      </header>

      {!data && loading && (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
        </div>
      )}

      {data && (
        <div className="space-y-6">
          <Card title="Current LLM">
            <div className="flex items-center gap-3 py-1">
              <div className="w-10 h-10 rounded-md bg-gradient-to-br from-violet-500/30 to-blue-500/30 flex items-center justify-center">
                <Sparkles className="w-4 h-4 text-violet-500" />
              </div>
              <div className="font-mono text-sm">{data.current_model}</div>
              <div className="ml-auto text-[11px] text-muted-foreground">
                Switch models in <code>HOMEOS_MODEL</code> env var
              </div>
            </div>
          </Card>

          <Card title={`Skills (${data.skills.length})`}>
            {data.skills.length === 0 ? (
              <EmptyMetric icon={Database} label="No skill invocations yet. Use the chat to call some skills." />
            ) : (
              <div className="divide-y divide-border">
                {data.skills.map((s, i) => {
                  const rate = s.n > 0 ? Math.round((s.successes / s.n) * 100) : 0;
                  const color = rate >= 80 ? "text-emerald-600" : rate >= 60 ? "text-amber-600" : "text-red-500";
                  return (
                    <div key={i} className="py-2.5 flex items-center gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium truncate">{s.skill_id}</div>
                        <div className="text-[10px] text-muted-foreground font-mono">
                          {s.llm_model || "(no model tag)"}
                          {s.avg_latency_ms && ` · ~${Math.round(s.avg_latency_ms)}ms`}
                        </div>
                      </div>
                      <div className={cn("font-mono text-sm font-semibold", color)}>{rate}%</div>
                      <div className="text-[11px] text-muted-foreground tabular-nums w-16 text-right">
                        {s.successes}/{s.n}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>

          <Card title={`Chat turns rated (${data.turns_by_model.reduce((a, t) => a + t.n, 0)})`}>
            {data.turns_by_model.length === 0 ? (
              <EmptyMetric icon={ThumbsUp} label="No turn ratings yet. The 👍/👎 buttons under assistant replies feed this." />
            ) : (
              <div className="space-y-2">
                {data.turns_by_model.map((t, i) => {
                  const total = t.up + t.down;
                  const rate = total > 0 ? Math.round((t.up / total) * 100) : 0;
                  return (
                    <div key={i} className="flex items-center gap-3 text-sm">
                      <div className="font-mono flex-1 min-w-0 truncate">{t.llm_model || "(no model)"}</div>
                      <span className="inline-flex items-center gap-0.5 text-emerald-500"><ThumbsUp className="w-3 h-3" />{t.up}</span>
                      <span className="inline-flex items-center gap-0.5 text-red-500"><ThumbsDown className="w-3 h-3" />{t.down}</span>
                      <span className="font-mono text-foreground/80 w-12 text-right">{rate}%</span>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>

          <Card title={`Templates (${data.templates.length})`}>
            {data.templates.length === 0 ? (
              <EmptyMetric icon={FileText} label="No template ratings yet." />
            ) : (
              <div className="space-y-2">
                {data.templates.map((t, i) => (
                  <div key={i} className="flex items-center gap-3 text-sm">
                    <div className="font-mono flex-1 min-w-0 truncate">{t.template_id}</div>
                    <span className="text-[10px] text-muted-foreground">{t.llm_model || "—"}</span>
                    <span className="inline-flex items-center gap-0.5 text-emerald-500"><ThumbsUp className="w-3 h-3" />{t.up}</span>
                    <span className="inline-flex items-center gap-0.5 text-red-500"><ThumbsDown className="w-3 h-3" />{t.down}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>

          {data.recent_failures.length > 0 && (
            <Card title="Recent failures">
              <div className="space-y-1.5">
                {data.recent_failures.map((f, i) => (
                  <div key={i} className="text-xs bg-red-500/5 border border-red-500/15 rounded-md p-2.5">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="font-mono font-medium text-red-600">{f.skill_id}</span>
                      <span className="text-[10px] text-muted-foreground">{f.created_at}</span>
                    </div>
                    <div className="text-[11px] text-muted-foreground mt-0.5 font-mono leading-snug line-clamp-2">{f.error}</div>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Connectors tab — deep-links to per-app management UIs ──────────

// ─── Users tab ────────────────────────────────────────────────────────
// Admin-only CRUD over user_profiles. Backed by /api/users
// (backend/users.py). "Invite" is admin-creates-credentials + a
// copy-to-clipboard handoff block — no token-link / SMTP email flow
// yet; that's a bigger feature.

interface YorikUserRow {
  id: number;
  name: string;
  email: string | null;
  role: string;
  language: string | null;
  disabled: boolean;
  created_at: string;
  last_login_at: string | null;
  active_sessions: number;
  has_password: number | boolean;
}

const ROLES = ["admin", "member", "child", "employee", "viewer"] as const;
type Role = typeof ROLES[number];

function UsersTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const auth = useAuth();
  const [users, setUsers] = useState<YorikUserRow[] | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [resetting, setResetting] = useState<YorikUserRow | null>(null);
  const [credentialsHandoff, setCredentialsHandoff] = useState<{
    name: string; email: string; password: string;
  } | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await api.get<YorikUserRow[]>("/api/users");
      setUsers(list);
    } catch (e: any) {
      setUsers([]);
      if (e?.status !== 403) toast("Failed to load users", "error");
    }
  }, [toast]);

  useEffect(() => { void load(); }, [load]);

  async function toggleDisabled(u: YorikUserRow) {
    try {
      await api.patch(`/api/users/${u.id}`, { disabled: !u.disabled });
      toast(`${u.name} ${u.disabled ? "enabled" : "disabled"}`, "success");
      await load();
    } catch (e: any) { toast(e?.message || "Failed", "error"); }
  }

  async function changeRole(u: YorikUserRow, role: Role) {
    if (role === u.role) return;
    try {
      await api.patch(`/api/users/${u.id}`, { role });
      toast(`${u.name} role → ${role}`, "success");
      await load();
    } catch (e: any) { toast(e?.message || "Failed", "error"); }
  }

  async function deleteUser(u: YorikUserRow) {
    if (!confirm(`Delete user "${u.name}"? This removes their profile and active sessions. Their data in shared tables (events, tasks) stays.`)) return;
    try {
      await api.delete(`/api/users/${u.id}`);
      toast(`${u.name} deleted`, "success");
      await load();
    } catch (e: any) { toast(e?.message || "Failed", "error"); }
  }

  return (
    <div>
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Users</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Family members, kids, employees. Each user gets their own login + their
            own view of bills/tasks based on role. Admin can do anything; viewer is read-only.
          </p>
        </div>
        <button
          onClick={() => setShowAdd(true)}
          className="px-3 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition inline-flex items-center gap-1.5 shrink-0"
        >
          <UserPlus className="w-4 h-4" /> Add user
        </button>
      </header>

      {users === null && (
        <div className="text-sm text-muted-foreground">Loading…</div>
      )}
      {users && users.length === 0 && (
        <div className="bg-card border border-border rounded-xl p-5 text-sm text-muted-foreground">
          Admin role required to manage users.
        </div>
      )}

      {users && users.length > 0 && (
        <div className="space-y-2">
          {users.map(u => (
            <UserRow
              key={u.id}
              user={u}
              isSelf={u.id === auth.user.id}
              onToggleDisabled={() => toggleDisabled(u)}
              onChangeRole={(r) => changeRole(u, r)}
              onResetPassword={() => setResetting(u)}
              onDelete={() => deleteUser(u)}
            />
          ))}
        </div>
      )}

      {showAdd && (
        <AddUserModal
          onClose={() => setShowAdd(false)}
          onCreated={(creds) => {
            setShowAdd(false);
            setCredentialsHandoff(creds);
            load();
          }}
          toast={toast}
        />
      )}

      {resetting && (
        <ResetPasswordModal
          user={resetting}
          onClose={() => setResetting(null)}
          onDone={(pw) => {
            setResetting(null);
            setCredentialsHandoff({
              name: resetting.name,
              email: resetting.email || "",
              password: pw,
            });
            load();
          }}
          toast={toast}
        />
      )}

      {credentialsHandoff && (
        <CredentialsHandoff
          name={credentialsHandoff.name}
          email={credentialsHandoff.email}
          password={credentialsHandoff.password}
          onClose={() => setCredentialsHandoff(null)}
          toast={toast}
        />
      )}
    </div>
  );
}

function UserRow({ user: u, isSelf, onToggleDisabled, onChangeRole, onResetPassword, onDelete }: {
  user: YorikUserRow;
  isSelf: boolean;
  onToggleDisabled: () => void;
  onChangeRole: (role: Role) => void;
  onResetPassword: () => void;
  onDelete: () => void;
}) {
  return (
    <div className={cn(
      "bg-card border border-border rounded-xl p-4 flex items-center gap-4",
      u.disabled && "opacity-60",
    )}>
      <div className="w-10 h-10 rounded-full bg-muted flex items-center justify-center font-semibold shrink-0">
        {(u.name || "?").charAt(0).toUpperCase()}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-semibold truncate">{u.name}</span>
          {isSelf && (
            <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-primary/10 text-primary">
              you
            </span>
          )}
          {u.disabled && (
            <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-rose-500/10 text-rose-600 dark:text-rose-400">
              disabled
            </span>
          )}
          {!u.has_password && (
            <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-600 dark:text-amber-400">
              no password
            </span>
          )}
        </div>
        <div className="text-xs text-muted-foreground truncate mt-0.5">
          {u.email || <em>no email</em>}
          {u.last_login_at && <span className="ml-2 opacity-70">· last login {formatRelativeShort(u.last_login_at)}</span>}
          {u.active_sessions > 0 && <span className="ml-2 opacity-70">· {u.active_sessions} session{u.active_sessions === 1 ? "" : "s"}</span>}
        </div>
      </div>
      <select
        value={u.role}
        onChange={e => onChangeRole(e.target.value as Role)}
        disabled={isSelf}
        className="h-8 px-2 rounded bg-muted text-xs focus:outline-none disabled:opacity-50"
        title={isSelf ? "Can't change your own role" : "Change role"}
      >
        {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
      </select>
      <button
        onClick={onResetPassword}
        title="Reset password"
        className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition"
      >
        <KeyRound className="w-4 h-4" />
      </button>
      <button
        onClick={onToggleDisabled}
        disabled={isSelf}
        title={isSelf ? "Can't disable yourself" : (u.disabled ? "Enable" : "Disable")}
        className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-30 disabled:cursor-not-allowed"
      >
        <Power className="w-4 h-4" />
      </button>
      <button
        onClick={onDelete}
        disabled={isSelf}
        title={isSelf ? "Can't delete yourself" : "Delete user"}
        className="p-1.5 rounded-md hover:bg-rose-500/10 text-muted-foreground hover:text-rose-600 transition disabled:opacity-30 disabled:cursor-not-allowed"
      >
        <Trash2 className="w-4 h-4" />
      </button>
    </div>
  );
}

function formatRelativeShort(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const ms = Date.now() - d.getTime();
  const days = Math.floor(ms / (24 * 3600 * 1000));
  if (days < 1) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function generatePassword(): string {
  // Easy-to-share but secure: three random Diceware-ish words + 3 digits.
  // No ambiguous chars in the digits. Avoids the "this looks scary" problem
  // of base64 random strings — the admin can read it over the phone if
  // they have to.
  const words = [
    "anker", "biber", "delta", "echo", "feder", "geist", "hafen", "iglu",
    "jade", "kran", "loro", "moos", "nebel", "ozean", "pinsel", "quark",
    "rabe", "salbei", "tundra", "ulme", "viper", "wolke", "xenon", "ypsilon", "zelle",
  ];
  const pick = () => words[Math.floor(Math.random() * words.length)];
  const digits = String(Math.floor(100 + Math.random() * 900));
  return `${pick()}-${pick()}-${pick()}-${digits}`;
}

function AddUserModal({ onClose, onCreated, toast }: {
  onClose: () => void;
  onCreated: (creds: { name: string; email: string; password: string }) => void;
  toast: (t: string, k?: "info" | "success" | "error") => void;
}) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [password, setPassword] = useState(() => generatePassword());
  const [provisionPaperless, setProvisionPaperless] = useState(true);
  const [provisionImmich, setProvisionImmich] = useState(true);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const auto_provision: string[] = [];
      if (provisionPaperless) auto_provision.push("paperless");
      if (provisionImmich) auto_provision.push("immich");
      await api.post("/api/users", { name, email, role, password, auto_provision });
      toast(`${name} added`, "success");
      onCreated({ name, email, password });
    } catch (e: any) {
      toast(e?.message || "Create failed", "error");
      setBusy(false);
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[800] flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
    >
      <form
        onSubmit={submit}
        className="w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden"
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-full bg-cyan-500/15 flex items-center justify-center">
              <UserPlus className="w-4 h-4 text-cyan-500" />
            </div>
            <div className="font-semibold">Add user</div>
          </div>
          <button type="button" onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-3">
          <Field label="Name">
            <input
              required
              autoFocus
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="Anna"
              className={inputClass}
            />
          </Field>
          <Field label="Email">
            <input
              required
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="anna@example.com"
              className={inputClass}
            />
          </Field>
          <Field label="Role">
            <select value={role} onChange={e => setRole(e.target.value as Role)} className={inputClass}>
              {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
          </Field>
          <Field label="Initial password">
            <div className="flex gap-1.5">
              <input
                required
                minLength={8}
                value={password}
                onChange={e => setPassword(e.target.value)}
                className={inputClass}
              />
              <button
                type="button"
                onClick={() => setPassword(generatePassword())}
                title="Generate a new one"
                className="px-2 rounded-md bg-muted hover:bg-muted/80 text-xs text-muted-foreground shrink-0"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            </div>
            <div className="text-[11px] text-muted-foreground mt-1 leading-relaxed">
              Share this with the user after they're added. They can change it themselves later.
            </div>
          </Field>
          <div className="border-t border-border pt-3 space-y-2">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                checked={provisionPaperless}
                onChange={e => setProvisionPaperless(e.target.checked)}
              />
              <span>Auto-create matching Paperless account</span>
            </label>
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                checked={provisionImmich}
                onChange={e => setProvisionImmich(e.target.checked)}
              />
              <span>Auto-create matching Immich account</span>
            </label>
            <div className="text-[11px] text-muted-foreground mt-1 leading-relaxed">
              Uses the same email + password. Skips silently if Paperless/Immich isn't configured.
            </div>
          </div>
        </div>
        <footer className="px-5 py-3 border-t border-border bg-muted/30 flex items-center justify-end gap-2">
          <button type="button" onClick={onClose} className="px-3 py-1.5 rounded-md text-sm hover:bg-muted transition">
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !name || !email || password.length < 8}
            className="px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition inline-flex items-center gap-1.5 disabled:opacity-50"
          >
            {busy ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <UserPlus className="w-3.5 h-3.5" />}
            Add user
          </button>
        </footer>
      </form>
    </div>,
    document.body,
  );
}

function ResetPasswordModal({ user, onClose, onDone, toast }: {
  user: YorikUserRow;
  onClose: () => void;
  onDone: (newPassword: string) => void;
  toast: (t: string, k?: "info" | "success" | "error") => void;
}) {
  const [password, setPassword] = useState(() => generatePassword());
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.post(`/api/users/${user.id}/reset-password`, { new_password: password });
      toast(`${user.name}'s password reset — their sessions were revoked`, "success");
      onDone(password);
    } catch (e: any) {
      toast(e?.message || "Reset failed", "error");
      setBusy(false);
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[800] flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <form onSubmit={submit} onClick={e => e.stopPropagation()}
        className="w-full max-w-sm bg-card border border-border rounded-2xl shadow-2xl overflow-hidden"
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <KeyRound className="w-4 h-4 text-primary" />
            <div className="font-semibold">Reset password for {user.name}</div>
          </div>
          <button type="button" onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-3">
          <Field label="New password">
            <div className="flex gap-1.5">
              <input required minLength={8} value={password} onChange={e => setPassword(e.target.value)} className={inputClass} autoFocus />
              <button type="button" onClick={() => setPassword(generatePassword())} className="px-2 rounded-md bg-muted hover:bg-muted/80 text-xs text-muted-foreground shrink-0">
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            </div>
          </Field>
          <div className="text-[11px] text-muted-foreground leading-relaxed">
            All of {user.name}'s active sessions will be revoked. They'll need to log in again with the new password.
          </div>
        </div>
        <footer className="px-5 py-3 border-t border-border bg-muted/30 flex items-center justify-end gap-2">
          <button type="button" onClick={onClose} className="px-3 py-1.5 rounded-md text-sm hover:bg-muted transition">Cancel</button>
          <button type="submit" disabled={busy || password.length < 8}
            className="px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition disabled:opacity-50">
            Reset password
          </button>
        </footer>
      </form>
    </div>,
    document.body,
  );
}

function CredentialsHandoff({ name, email, password, onClose, toast }: {
  name: string; email: string; password: string;
  onClose: () => void;
  toast: (t: string, k?: "info" | "success" | "error") => void;
}) {
  const loginUrl = typeof window !== "undefined" ? `${window.location.origin}/` : "/";
  const block =
`Hi ${name},
I added you to Yorik (our home OS).
Sign in at: ${loginUrl}
Email:    ${email}
Password: ${password}
You can change the password once you're in (Settings → Profile).`;

  async function copyAll() {
    try {
      await navigator.clipboard.writeText(block);
      toast("Copied — paste in Signal / message", "success");
    } catch {
      toast("Clipboard not available — select + copy manually", "error");
    }
  }
  async function copyOne(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      toast(`${label} copied`, "success");
    } catch {/* ignore */}
  }

  return createPortal(
    <div className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4" onClick={onClose}>
      <div className="w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden" onClick={e => e.stopPropagation()}>
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <CheckCircle2 className="w-4 h-4 text-emerald-500" />
            <div className="font-semibold">Share these with {name}</div>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-3">
          <p className="text-sm text-muted-foreground leading-relaxed">
            Yorik doesn't email invites yet — send these to {name} however you normally chat
            (Signal, WhatsApp, paper note). They'll log in and can change the password
            themselves.
          </p>
          <div className="bg-muted/50 rounded-lg p-3 text-sm font-mono space-y-1.5">
            <CredRow label="URL"      value={loginUrl}  onCopy={() => copyOne(loginUrl, "URL")} />
            <CredRow label="Email"    value={email}     onCopy={() => copyOne(email, "Email")} />
            <CredRow label="Password" value={password}  onCopy={() => copyOne(password, "Password")} mono />
          </div>
          <button
            onClick={copyAll}
            className="w-full px-3 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition inline-flex items-center justify-center gap-1.5"
          >
            <Copy className="w-3.5 h-3.5" />
            Copy ready-to-send message
          </button>
          <div className="text-[11px] text-muted-foreground leading-relaxed text-center">
            The password is shown only once. Closing this dialog forgets it; you'd have to reset.
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

function CredRow({ label, value, onCopy, mono }: {
  label: string; value: string; onCopy: () => void; mono?: boolean;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold w-16 shrink-0">{label}</span>
      <span className={cn("flex-1 truncate", mono && "font-mono")}>{value}</span>
      <button onClick={onCopy} className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground transition" title={`Copy ${label}`}>
        <Copy className="w-3 h-3" />
      </button>
    </div>
  );
}


// ─── Apps tab ─────────────────────────────────────────────────────────
// Opt-in apps (currently WhatsApp) ship registered-but-hidden. They appear
// here so the user can flip them on. Once enabled, they show up in the
// dock and on the home grid like any other bundled app. The toggle is
// instant — no process restart needed because the gate is evaluated on
// every /api/apps request.

interface OptInApp {
  id: string;
  name: string;
  icon: string;
  description: string;
  enabled: boolean;
}

function AppsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [apps, setApps] = useState<OptInApp[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await api.get<OptInApp[]>("/api/apps/opt-in");
      // Defensive: if the API ever returns a non-array (e.g. an SPA HTML
      // fallback because of a routing mishap), don't crash render with
      // `.map is not a function`. Show an empty state instead.
      setApps(Array.isArray(list) ? list : []);
    } catch (e: any) {
      // 403 here means the current user isn't admin. Empty list + a hint
      // is friendlier than a red toast on a tab they navigated to manually.
      setApps([]);
      if (e?.status && e.status !== 403) toast("Failed to load apps list", "error");
    }
  }, [toast]);

  useEffect(() => { void load(); }, [load]);

  async function toggle(appId: string, next: boolean) {
    setBusy(appId);
    try {
      await api.post(`/api/apps/${appId}/${next ? "enable" : "disable"}`);
      setApps(curr => curr?.map(a => a.id === appId ? { ...a, enabled: next } : a) ?? curr);
      toast(`${appId} ${next ? "enabled" : "disabled"} — refresh the dock to see the change`, "success");
    } catch {
      toast(`Failed to ${next ? "enable" : "disable"} ${appId}`, "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Apps</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Optional apps that ship with Yorik but stay hidden until you turn
          them on. They depend on external services you set up yourself
          (e.g. WhatsApp needs the Baileys bridge container running).
        </p>
      </header>

      {apps === null && (
        <div className="text-sm text-muted-foreground">Loading…</div>
      )}

      {apps && apps.length === 0 && (
        <div className="bg-card border border-border rounded-xl p-5 text-sm text-muted-foreground">
          No optional apps available. (Admin role required to manage these.)
        </div>
      )}

      {apps && apps.length > 0 && (
        <div className="space-y-3">
          {apps.map(a => (
            <div
              key={a.id}
              className="bg-card border border-border rounded-xl overflow-hidden"
            >
              <div className="p-5 flex items-start gap-4">
                <div className="text-2xl shrink-0">{a.icon}</div>
                <div className="flex-1 min-w-0">
                  <div className="font-semibold">{a.name}</div>
                  <div className="text-sm text-muted-foreground mt-1 leading-relaxed">
                    {a.description}
                  </div>
                </div>
                <button
                  onClick={() => toggle(a.id, !a.enabled)}
                  disabled={busy === a.id}
                  className={cn(
                    "shrink-0 relative inline-flex h-6 w-11 items-center rounded-full transition",
                    a.enabled ? "bg-primary" : "bg-muted",
                    busy === a.id && "opacity-50 cursor-wait",
                  )}
                  aria-pressed={a.enabled}
                  aria-label={`${a.enabled ? "Disable" : "Enable"} ${a.name}`}
                >
                  <span
                    className={cn(
                      "inline-block h-5 w-5 transform rounded-full bg-background shadow transition",
                      a.enabled ? "translate-x-[22px]" : "translate-x-0.5",
                    )}
                  />
                </button>
              </div>
              {/* Per-app options. Only WhatsApp has any so far — extend
                  with switch on a.id when more apps grow knobs. */}
              {a.id === "whatsapp" && a.enabled && (
                <WhatsAppOptions toast={toast} />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


// ─── Marketplace tab ──────────────────────────────────────────────────
// Community apps published in marketplace/catalog.json. Each entry
// installs into apps/<id>/ via /api/apps/install_from_catalog/{id}
// and runs the normal app-loader lifecycle. Installed apps appear in
// the Dock; uninstall here or via DELETE /api/apps/{id}.

interface MarketplaceApp {
  id: string;
  name: string;
  description: string;
  icon: string;
  version: string;
  author: string;
  license: string;
  homepage: string;
  tags: string[];
  installed: boolean;
  // Permissions the app declared in its manifest. Empty arrays mean
  // "fully self-contained — own DB, no cross-app access."
  requires_tables_external?: { db?: string; table?: string; access?: string }[];
  requires_connectors?: string[];
}

// Authors trusted as first-party. Apps with these IDs in manifest.author
// get the Verified badge. Anything else is treated as community / unvetted.
// Future: replace with a cryptographic signature check.
const VERIFIED_AUTHORS = new Set(["yorik-core", "yorik"]);

function MarketplaceTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [apps, setApps] = useState<MarketplaceApp[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pending, setPending] = useState<MarketplaceApp | null>(null);
  const [pendingUninstall, setPendingUninstall] = useState<MarketplaceApp | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await api.get<MarketplaceApp[]>("/api/apps/available");
      setApps(Array.isArray(list) ? list : []);
    } catch (e: any) {
      setApps([]);
      if (e?.status && e.status !== 403) toast("Failed to load marketplace", "error");
    }
  }, [toast]);

  useEffect(() => { void load(); }, [load]);

  async function doInstall(app: MarketplaceApp) {
    setPending(null);
    setBusy(app.id);
    try {
      await api.post(`/api/apps/install_from_catalog/${app.id}`);
      toast(`${app.name} installed — open the Dock to use it`, "success");
      await load();
    } catch {
      toast(`Failed to install ${app.name}`, "error");
    } finally {
      setBusy(null);
    }
  }

  async function doUninstall(app: MarketplaceApp) {
    setPendingUninstall(null);
    setBusy(app.id);
    try {
      await api.delete(`/api/apps/${app.id}?wipe_data=true`);
      toast(`${app.name} uninstalled`, "success");
      await load();
    } catch {
      toast(`Failed to uninstall ${app.name}`, "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Marketplace</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Community apps that extend Yorik. Each one ships its own database
          and operations the Yorik agent can call.
        </p>
      </header>

      {/* Safety intro — what the sandbox actually does. */}
      <div className="mb-5 rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4 flex gap-3">
        <Shield className="w-4 h-4 text-emerald-500 shrink-0 mt-0.5" />
        <div className="text-[13px] leading-relaxed">
          <div className="font-medium text-foreground">Apps run sandboxed.</div>
          <div className="text-muted-foreground mt-0.5">
            Each app gets its own private database and a sandboxed iframe with no
            network access. It cannot read your other Yorik data unless its
            manifest declares it and you approve at install. <span className="inline-flex items-center gap-1"><BadgeCheck className="w-3 h-3 text-emerald-600 inline" /> Verified</span> apps
            are first-party and reviewed by the Yorik maintainers.
          </div>
        </div>
      </div>

      {apps === null && (
        <div className="text-sm text-muted-foreground">Loading…</div>
      )}

      {apps && apps.length === 0 && (
        <div className="bg-card border border-border rounded-xl p-8 text-sm text-muted-foreground text-center">
          <Store className="w-6 h-6 mx-auto mb-2 opacity-50" />
          <div>No apps in the catalog yet.</div>
          <div className="mt-1 text-xs">Check back as new community apps are vetted and added.</div>
        </div>
      )}

      {apps && apps.length > 0 && (
        <div className="space-y-3">
          {apps.map(a => {
            const verified = VERIFIED_AUTHORS.has(a.author);
            return (
              <div
                key={a.id}
                className="bg-card border border-border rounded-xl p-5 flex items-start gap-4"
              >
                <div className="text-2xl shrink-0">{a.icon}</div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <div className="font-semibold">{a.name}</div>
                    <span className="text-xs text-muted-foreground">v{a.version}</span>
                    {a.author && (
                      <span className="text-xs text-muted-foreground">by {a.author}</span>
                    )}
                    {verified && (
                      <span
                        className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                        title="First-party app, reviewed by Yorik maintainers"
                      >
                        <BadgeCheck className="w-3 h-3" />
                        Verified
                      </span>
                    )}
                  </div>
                  <div className="text-sm text-muted-foreground mt-1 leading-relaxed">
                    {a.description}
                  </div>
                  {a.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {a.tags.map(t => (
                        <span key={t} className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
                          {t}
                        </span>
                      ))}
                    </div>
                  )}
                  {a.homepage && (
                    <a
                      href={a.homepage}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-xs text-primary hover:underline mt-2"
                    >
                      Source <ExternalLink className="w-3 h-3" />
                    </a>
                  )}
                </div>
                {a.installed ? (
                  <button
                    onClick={() => setPendingUninstall(a)}
                    disabled={busy === a.id}
                    className={cn(
                      "shrink-0 px-3 py-1.5 rounded-md text-sm font-medium border border-border",
                      "hover:bg-muted text-muted-foreground hover:text-foreground transition",
                      busy === a.id && "opacity-50 cursor-wait",
                    )}
                  >
                    {busy === a.id ? "Uninstalling…" : "Uninstall"}
                  </button>
                ) : (
                  <button
                    onClick={() => setPending(a)}
                    disabled={busy === a.id}
                    className={cn(
                      "shrink-0 px-3 py-1.5 rounded-md text-sm font-medium bg-primary text-primary-foreground",
                      "hover:bg-primary/90 transition inline-flex items-center gap-1.5",
                      busy === a.id && "opacity-50 cursor-wait",
                    )}
                  >
                    <Download className="w-3.5 h-3.5" />
                    {busy === a.id ? "Installing…" : "Install"}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {pending && (
        <InstallConfirmModal
          app={pending}
          verified={VERIFIED_AUTHORS.has(pending.author)}
          onClose={() => setPending(null)}
          onConfirm={() => doInstall(pending)}
        />
      )}
      {pendingUninstall && (
        <UninstallConfirmModal
          app={pendingUninstall}
          onClose={() => setPendingUninstall(null)}
          onConfirm={() => doUninstall(pendingUninstall)}
        />
      )}
    </div>
  );
}


// Pre-install permissions disclosure. Even apps with no grants make
// the user click through, so install never feels like a stealth action.

function InstallConfirmModal({ app, verified, onClose, onConfirm }: {
  app: MarketplaceApp;
  verified: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const tables = app.requires_tables_external || [];
  const connectors = app.requires_connectors || [];
  const selfContained = tables.length === 0 && connectors.length === 0;

  return createPortal(
    <div
      className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="font-semibold">Install {app.name}?</div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="p-5 space-y-4">
          <div className="flex items-start gap-3">
            <div className="text-3xl shrink-0">{app.icon}</div>
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-semibold">{app.name}</span>
                <span className="text-xs text-muted-foreground">v{app.version}</span>
                {verified && (
                  <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-700 dark:text-emerald-400">
                    <BadgeCheck className="w-3 h-3" />
                    Verified
                  </span>
                )}
              </div>
              {app.author && (
                <div className="text-xs text-muted-foreground mt-0.5">by {app.author}</div>
              )}
              <p className="text-sm text-muted-foreground mt-2 leading-relaxed">{app.description}</p>
            </div>
          </div>

          <div className="border-t border-border pt-4">
            <div className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground mb-2">
              What this app can access
            </div>
            <ul className="space-y-1.5 text-[13px]">
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
                <span>
                  Its own private database
                  <span className="font-mono text-[11px] text-muted-foreground"> (data/apps/{app.id}/data.db)</span>
                </span>
              </li>
              {tables.length > 0 && tables.map((t, i) => (
                <li key={i} className="flex items-start gap-2">
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
                  <span>
                    <strong>{t.access || "read"}</strong> access to
                    <span className="font-mono text-[11px] ml-1">{t.db}.{t.table}</span>
                  </span>
                </li>
              ))}
              {connectors.length > 0 && connectors.map((c, i) => (
                <li key={i} className="flex items-start gap-2">
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
                  <span>Calls the <span className="font-mono text-[11px]">{c}</span> connector</span>
                </li>
              ))}
              {selfContained && (
                <li className="flex items-start gap-2 text-muted-foreground">
                  <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
                  <span>The Yorik LLM (text completions via the SDK)</span>
                </li>
              )}
            </ul>
          </div>

          <div className="border-t border-border pt-4">
            <div className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground mb-2">
              Sandboxed away from
            </div>
            <ul className="space-y-1 text-[12.5px] text-muted-foreground">
              {tables.length === 0 && (
                <li className="flex items-center gap-2">
                  <Lock className="w-3 h-3 shrink-0" />
                  Your calendar, tasks, bills, contacts, emails, documents
                </li>
              )}
              <li className="flex items-center gap-2">
                <Globe className="w-3 h-3 shrink-0" />
                Outbound network — the iframe has CSP <code className="font-mono text-[10px]">connect-src 'none'</code>
              </li>
              <li className="flex items-center gap-2">
                <Lock className="w-3 h-3 shrink-0" />
                Other apps' data and your session cookie
              </li>
            </ul>
          </div>

          <div className="flex gap-2 pt-2">
            <button
              onClick={onClose}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium border border-border hover:bg-muted text-muted-foreground hover:text-foreground transition"
            >
              Cancel
            </button>
            <button
              onClick={onConfirm}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium bg-primary text-primary-foreground hover:bg-primary/90 transition inline-flex items-center justify-center gap-1.5"
            >
              <Download className="w-3.5 h-3.5" />
              Install {app.name}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}


function UninstallConfirmModal({ app, onClose, onConfirm }: {
  app: MarketplaceApp;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return createPortal(
    <div
      className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm bg-card border border-border rounded-2xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="font-semibold">Uninstall {app.name}?</div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-4">
          <div className="text-sm text-muted-foreground leading-relaxed">
            This removes <span className="font-mono text-[12px]">{app.id}</span> and
            <strong className="text-foreground"> wipes its database</strong> at
            <span className="font-mono text-[11px]"> data/apps/{app.id}/</span>.
          </div>
          <div className="text-[12px] text-muted-foreground">
            You can reinstall from the Marketplace at any time, but the data will not return.
          </div>
          <div className="flex gap-2 pt-2">
            <button
              onClick={onClose}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium border border-border hover:bg-muted transition"
            >
              Keep installed
            </button>
            <button
              onClick={onConfirm}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium bg-destructive text-white hover:opacity-90 transition"
            >
              Uninstall and wipe
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}


function WhatsAppOptions({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get<{ import_status_broadcasts: boolean }>("/api/whatsapp/settings")
      .then(r => setEnabled(r.import_status_broadcasts))
      .catch(() => setEnabled(false));
  }, []);

  async function flip(next: boolean) {
    setBusy(true);
    try {
      const r = await api.patch<{ import_status_broadcasts: boolean }>(
        "/api/whatsapp/settings", { import_status_broadcasts: next });
      setEnabled(r.import_status_broadcasts);
      toast(next
        ? "Status posts will now import to Photos"
        : "Status posts will no longer import", "success");
    } catch {
      toast("Failed to update setting", "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border-t border-border bg-muted/20 px-5 py-3 flex items-start gap-4">
      <div className="flex-1 text-sm">
        <div className="font-medium">Import Status posts to Photos</div>
        <div className="text-xs text-muted-foreground mt-0.5 leading-relaxed">
          Off by default. Your contacts' 24-hour Status photos won't be
          auto-saved to Immich. Direct chats + groups always import as before.
        </div>
      </div>
      <button
        onClick={() => flip(!enabled)}
        disabled={busy || enabled === null}
        className={cn(
          "shrink-0 relative inline-flex h-5 w-9 items-center rounded-full transition mt-1",
          enabled ? "bg-primary" : "bg-muted",
          (busy || enabled === null) && "opacity-50 cursor-wait",
        )}
        aria-pressed={!!enabled}
      >
        <span
          className={cn(
            "inline-block h-4 w-4 transform rounded-full bg-background shadow transition",
            enabled ? "translate-x-[18px]" : "translate-x-0.5",
          )}
        />
      </button>
    </div>
  );
}


function ConnectorsTab() {
  return (
    <div className="space-y-4">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Connectors</h1>
        <p className="text-sm text-muted-foreground mt-1">
          External services Yorik can call. Most are configured at install
          time by <code>start.sh</code> (Immich, Paperless, n8n) or per-user
          via the app that uses them.
        </p>
      </header>

      {/* Email is the only one with a real interactive management UI right
          now, and it lives inside the Email app so add/edit/remove all
          happen against the multi-account `email_accounts` store — not
          the old single-credential connector store this tab used to
          mirror (which would have left the Email app oblivious). */}
      <DeepLinkCard
        title="Email accounts"
        description="Add, edit, or remove IMAP/SMTP accounts (you can have multiple). Managed inside the Email app so the multi-account store stays the single source of truth."
        href="/r/email"
        cta="Open Email →"
      />

      <MapsConnectorCard />
    </div>
  );
}


function MapsConnectorCard() {
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [configured, setConfigured] = useState<boolean | null>(null);

  // Probe whether an ors_api_key is already stored. The list endpoint
  // intentionally only exposes connector names + updated_at — never the
  // payload — so we can only show "configured" vs "not", not the value.
  useEffect(() => {
    let cancelled = false;
    api.get<Array<{ connector_name: string; updated_at: string }>>("/api/connectors/credentialed")
      .then(rows => {
        if (cancelled) return;
        setConfigured(rows.some(r => r.connector_name === "maps"));
      })
      .catch(() => { if (!cancelled) setConfigured(false); });
    return () => { cancelled = true; };
  }, []);

  async function save() {
    const key = apiKey.trim();
    if (!key) {
      setStatus("Please enter an API key.");
      return;
    }
    setBusy(true);
    setStatus(null);
    try {
      await api.post("/api/connectors/maps/credentials", { credentials: { ors_api_key: key } });
      setStatus("✓ Saved. Yorik now uses OpenRouteService for route calculations.");
      setConfigured(true);
      setApiKey("");
    } catch (err: any) {
      setStatus(`Error: ${err?.message || err}`);
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    if (!confirm("Remove the OpenRouteService API key? Yorik will fall back to the free OSRM demo.")) return;
    setBusy(true);
    setStatus(null);
    try {
      await api.delete("/api/connectors/maps/credentials");
      setStatus("✓ Removed. Routing is using the OSRM demo again.");
      setConfigured(false);
    } catch (err: any) {
      setStatus(`Error: ${err?.message || err}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <div className="flex items-start gap-3 mb-3">
        <div className="flex-1 min-w-0">
          <div className="font-semibold">Maps (OpenStreetMap + optional OpenRouteService)</div>
          <div className="text-sm text-muted-foreground mt-1 leading-relaxed">
            Works <em>without</em> an API key via the free OSM services
            (Nominatim for geocoding, OSRM demo for routing, Overpass for
            POI search). With an OpenRouteService API key, Yorik gets
            better routing accuracy and ~2,000 requests/day.
          </div>
        </div>
        {configured !== null && (
          <span className={cn(
            "text-[11px] px-2 py-0.5 rounded shrink-0",
            configured
              ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
              : "bg-muted text-muted-foreground"
          )}>
            {configured ? "ORS active" : "OSRM demo"}
          </span>
        )}
      </div>

      <Field label="OpenRouteService API-Key (optional)">
        <input
          type="password"
          value={apiKey}
          onChange={e => setApiKey(e.target.value)}
          placeholder={configured ? "Current key is stored encrypted — enter a new one to overwrite" : "e.g. 5b3ce3597851110001cf6248..."}
          className={cn(inputClass, "font-mono")}
          disabled={busy}
        />
      </Field>

      <div className="flex items-center gap-2 mt-3">
        <button
          onClick={save}
          disabled={busy || !apiKey.trim()}
          className="text-sm px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 transition"
        >
          {busy ? "Saving…" : "Save"}
        </button>
        {configured && (
          <button
            onClick={clear}
            disabled={busy}
            className="text-sm px-3 py-1.5 rounded-md border border-border hover:bg-muted disabled:opacity-50 transition"
          >
            Remove
          </button>
        )}
        <a
          href="https://openrouteservice.org/dev/#/signup"
          target="_blank" rel="noopener"
          className="text-xs text-muted-foreground hover:text-foreground underline ml-auto"
        >
          Get a free key →
        </a>
      </div>

      {status && (
        <div className={cn(
          "mt-3 text-xs",
          status.startsWith("✓") ? "text-emerald-600 dark:text-emerald-400" : "text-amber-600"
        )}>
          {status}
        </div>
      )}
    </div>
  );
}


function DeepLinkCard({
  title, description, href, cta,
}: {
  title: string;
  description: string;
  href: string;
  cta: string;
}) {
  return (
    <div className="bg-card border border-border rounded-xl p-5 flex items-start gap-4">
      <div className="flex-1 min-w-0">
        <div className="font-semibold">{title}</div>
        <div className="text-sm text-muted-foreground mt-1 leading-relaxed">
          {description}
        </div>
      </div>
      <a
        href={href}
        className="inline-flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:opacity-90 transition shrink-0"
      >
        {cta}
      </a>
    </div>
  );
}

// ─── Logs ──────────────────────────────────────────────────────────────
// Surfaces the persisted WARNING+ error_log table — same data that
// `tail -f data/logs/yorik.log | grep WARNING` would show, but in
// the app so a tester can see "what's been breaking" without SSHing
// into the host. Backend is admin-gated; non-admin hits surface as
// a friendly 403 message in the tab.

type ErrorRow = {
  id: number;
  ts: string;
  level: string;
  logger: string;
  message: string;
  traceback: string | null;
  request_path: string | null;
  corr_id: string | null;
};

type ErrorsResponse = {
  errors: ErrorRow[];
  summary: Record<string, number>;
};

// ─── Extensions tab ────────────────────────────────────────────────────
// Optional Python modules that opt into capabilities the base install
// doesn't carry — currently the ZUGFeRD / Factur-X e-invoicing payload
// embedder. Each extension declares its pip dependencies in
// extensions/<id>/requirements.txt; admin clicks Install and Yorik pip-
// installs them into the running venv. Requires uvicorn restart for the
// extension to actually load its Python module (we tell the user that).
interface ExtensionRow {
  id: string;
  name: string;
  description: string;
  version: string;
  author?: string;
  country?: string;
  tags?: string[];
  python_requirements: string[];
  deps: { all_met: boolean; missing: string[]; present: string[] };
  loaded: boolean;
  errors: string[];
  docs_url?: string;
}

function ExtensionsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [rows, setRows] = useState<ExtensionRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [installing, setInstalling] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.get<ExtensionRow[]>("/api/extensions");
      setRows(data || []);
      setForbidden(false);
    } catch (e: any) {
      if (String(e?.message || "").includes("403")) setForbidden(true);
      else toast(`Couldn't load extensions: ${e.message}`, "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { refresh(); }, [refresh]);

  async function install(extId: string) {
    setInstalling(extId);
    try {
      const r = await api.post<{ ok: boolean; returncode?: number; stdout?: string; stderr?: string; error?: string }>(
        `/api/extensions/${extId}/install`, {},
      );
      if (r.ok) {
        toast(
          `Installed ${extId} — restart the Yorik backend (Settings → Backup ... → Restart, or rerun start.sh) for the extension to load.`,
          "success",
        );
      } else {
        toast(`Install failed: ${r.error || `pip exit ${r.returncode}`}`, "error");
      }
      await refresh();
    } catch (e: any) {
      toast(e?.message || "Install failed", "error");
    } finally {
      setInstalling(null);
    }
  }

  if (forbidden) {
    return (
      <div className="text-sm text-muted-foreground py-8 text-center">
        Admin only. Ask an admin user to install extensions.
      </div>
    );
  }

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Extensions</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Optional Python modules that add capabilities the base install
          doesn't carry — typically locale- or compliance-specific (e.g.
          German e-invoicing). Each one declares its own pip dependencies
          so the base install stays lean for users who don't need them.
        </p>
        <p className="text-[11px] text-amber-600 mt-2 flex items-start gap-1.5">
          <AlertCircle className="w-3 h-3 mt-0.5 shrink-0" />
          After installing dependencies, the Yorik backend must be
          restarted (rerun <code className="font-mono">bash start.sh</code>)
          before the extension's hooks become active.
        </p>
      </header>

      {loading && !rows && (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
        </div>
      )}

      {rows && rows.length === 0 && (
        <div className="text-sm text-muted-foreground py-8 text-center">
          No extensions discovered in <code>extensions/</code>.
        </div>
      )}

      {rows && rows.length > 0 && (
        <div className="space-y-3">
          {rows.map(r => (
            <Card key={r.id} title={undefined}>
              <div className="flex items-start gap-3">
                <Puzzle className="w-5 h-5 text-indigo-500 mt-0.5 shrink-0" />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <h3 className="font-medium text-sm">{r.name}</h3>
                    <span className="text-[10px] text-muted-foreground font-mono">v{r.version}</span>
                    {r.country && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted/60 text-muted-foreground">{r.country}</span>
                    )}
                    {r.loaded ? (
                      <span className="text-[10px] inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 border border-emerald-500/20">
                        <CheckCircle2 className="w-2.5 h-2.5" /> active
                      </span>
                    ) : r.deps.all_met ? (
                      <span className="text-[10px] inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-600 border border-amber-500/20">
                        <AlertCircle className="w-2.5 h-2.5" /> installed, restart pending
                      </span>
                    ) : (
                      <span className="text-[10px] inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground border border-border">
                        <X className="w-2.5 h-2.5" /> dependencies not installed
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1.5">{r.description}</p>
                  {r.python_requirements.length > 0 && (
                    <div className="mt-2 text-[10px] text-muted-foreground">
                      <span className="font-medium">Python deps:</span>{" "}
                      <span className="font-mono">{r.python_requirements.join(", ")}</span>
                    </div>
                  )}
                  {r.errors.length > 0 && (
                    <div className="mt-2 text-[10px] text-rose-600">
                      <span className="font-medium">Errors:</span> {r.errors.join(" / ")}
                    </div>
                  )}
                  {r.docs_url && (
                    <a
                      href={r.docs_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-[10px] text-blue-500 hover:underline inline-flex items-center gap-1 mt-2"
                    >
                      <ExternalLink className="w-2.5 h-2.5" /> Docs
                    </a>
                  )}
                </div>
                <div className="shrink-0">
                  {!r.deps.all_met && (
                    <button
                      type="button"
                      onClick={() => install(r.id)}
                      disabled={installing === r.id}
                      className={cn(
                        "px-3 py-1.5 text-xs rounded-md font-medium inline-flex items-center gap-1.5 transition",
                        "bg-gradient-to-r from-indigo-500 to-violet-500 hover:from-indigo-600 hover:to-violet-600 text-white shadow-sm",
                        "disabled:opacity-60 disabled:cursor-wait",
                      )}
                    >
                      {installing === r.id
                        ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        : <Upload className="w-3.5 h-3.5" />}
                      {installing === r.id ? "Installing…" : "Install"}
                    </button>
                  )}
                  {r.deps.all_met && !r.loaded && (
                    <button
                      type="button"
                      onClick={refresh}
                      className="px-3 py-1.5 text-xs rounded-md font-medium bg-muted hover:bg-muted/70 inline-flex items-center gap-1.5"
                    >
                      <RefreshCw className="w-3.5 h-3.5" /> Re-check
                    </button>
                  )}
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}


// ─── Extractions tab — contacts proposed from Paperless docs ──────────
// Surfaces contact_extraction_proposals (per-doc) so admin can accept
// (create new contact OR merge into a suggested existing one) or
// reject. The background worker in backend/contact_extractor.py
// populates the queue every 6 h; this panel is the human-in-the-loop
// review step. Admin only (the underlying endpoints 403 for non-admin
// AND the Settings tab itself is gated via adminOnly above).

interface ExtractionItem {
  id:                      number;
  source_paperless_doc_id: number;
  proposed: {
    display_name?:     string;
    business_name?:    string;
    legal_name?:       string;
    kind?:             "person" | "business";
    address_street?:   string;
    address_postcode?: string;
    address_city?:     string;
    address_country?:  string;
    salutation_pref?:  string;
    iban?:             string;
    tax_id?:           string;
    emails?:           string[];
    phones?:           string[];
    source_snippet?:   string;
  };
  match_candidate_id:  number | null;
  match_display_name:  string | null;
  match_kind:          string | null;
  match_score:         number | null;
  match_reason:        string | null;
  status:              "pending" | "accepted" | "rejected" | "merged" | "skipped";
  created_contact_id:  number | null;
  created_at:          string;
  decided_at:          string | null;
}

interface ExtractionsResponse {
  items:  ExtractionItem[];
  counts: Record<string, number>;
}

function ExtractionsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [data, setData] = useState<ExtractionsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<"pending" | "all" | "accepted" | "rejected" | "merged" | "skipped">("pending");
  // Per-item busy + collapse state.
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const [expanded, setExpanded] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.get<ExtractionsResponse>(`/api/contact-extractions?status=${filter}&limit=200`);
      setData(r);
    } catch (e: any) {
      // ApiError stringifies cleanly via .toString() / .message; only
      // when something throws a bare object does the prior template
      // string land as "[object Object]". Coerce to string defensively.
      const msg = typeof e === "string"
        ? e
        : (e?.message || e?.toString?.() || JSON.stringify(e));
      toast(`Couldn't load extractions: ${msg}`, "error");
    } finally {
      setLoading(false);
    }
  }, [filter, toast]);

  useEffect(() => { refresh(); }, [refresh]);

  async function decide(item: ExtractionItem, decision: "accept_create" | "accept_merge" | "reject", mergeId?: number) {
    setBusy(b => { const n = new Set(b); n.add(item.id); return n; });
    try {
      await api.post(`/api/contact-extractions/${item.id}/decide`, {
        decision,
        merge_into_contact_id: mergeId ?? null,
      });
      const label = decision === "accept_create" ? "created"
                   : decision === "accept_merge"  ? "merged"
                   : "rejected";
      const name = item.proposed.display_name || item.proposed.business_name || `proposal #${item.id}`;
      toast(`${name}: ${label}`, "success");
      await refresh();
    } catch (e: any) {
      toast(`Action failed: ${e.message}`, "error");
    } finally {
      setBusy(b => { const n = new Set(b); n.delete(item.id); return n; });
    }
  }

  const counts = data?.counts || {};
  const pending = counts.pending || 0;

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Extractions</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Contacts Yorik proposed from your Paperless documents.
          The worker walks every doc once, runs a regex pass for IBAN /
          email / phone / tax-id and a short LLM call for the sender block,
          and either suggests merging into an existing contact (when a match
          looks plausible) or creating a brand-new one.
        </p>
      </header>

      <Card title={`Queue (${pending} pending)`}>
        <div className="flex items-center gap-2 mb-3">
          {(["pending", "accepted", "rejected", "merged", "skipped", "all"] as const).map(f => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={cn(
                "text-xs px-2.5 py-1 rounded-md transition",
                filter === f
                  ? "bg-violet-500 text-white"
                  : "bg-muted/40 text-muted-foreground hover:bg-muted/60",
              )}
            >
              {f}
              <span className="ml-1 opacity-70 tabular-nums">
                {counts[f] ?? (f === "all" ? Object.values(counts).reduce((a, b) => a + b, 0) : 0)}
              </span>
            </button>
          ))}
          <button
            onClick={refresh}
            disabled={loading}
            className="ml-auto text-xs px-2.5 py-1 rounded-md bg-muted/40 hover:bg-muted/60 transition flex items-center gap-1"
          >
            <RefreshCw className={cn("w-3 h-3", loading && "animate-spin")} /> Refresh
          </button>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-8 text-muted-foreground">
            <Loader2 className="w-5 h-5 animate-spin" />
          </div>
        ) : !data?.items.length ? (
          <p className="text-xs text-muted-foreground py-4">
            Nothing in this bucket. {filter === "pending" && "The worker ticks every 6 hours — new proposals will appear after the next pass."}
          </p>
        ) : (
          <div className="space-y-1.5">
            {data.items.map(item => {
              const isOpen = expanded === item.id;
              const isBusy = busy.has(item.id);
              const p = item.proposed;
              const name = p.display_name || p.business_name || p.legal_name || "(no name)";
              const kind = p.kind || (p.business_name ? "business" : "person");

              return (
                <div key={item.id} className={cn(
                  "border border-border rounded-md bg-card overflow-hidden",
                  item.status !== "pending" && "opacity-60",
                )}>
                  <button
                    onClick={() => setExpanded(isOpen ? null : item.id)}
                    className="w-full text-left px-3 py-2 hover:bg-muted/30 transition"
                  >
                    <div className="flex items-center gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium truncate">{name}</span>
                          <span className="text-[9px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground uppercase tracking-wider">{kind}</span>
                          {item.match_candidate_id && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-600">
                              looks like {item.match_display_name} ({item.match_reason})
                            </span>
                          )}
                          {item.status !== "pending" && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-violet-500/15 text-violet-500 uppercase tracking-wider">
                              {item.status}
                            </span>
                          )}
                        </div>
                        <div className="text-[11px] text-muted-foreground mt-0.5 flex gap-3">
                          {p.iban   && <span className="font-mono">{p.iban}</span>}
                          {p.tax_id && <span className="font-mono">{p.tax_id}</span>}
                          {p.emails?.[0] && <span className="font-mono">{p.emails[0]}</span>}
                          {p.address_city && <span>{p.address_city}</span>}
                          <span className="opacity-60">doc #{item.source_paperless_doc_id}</span>
                        </div>
                      </div>
                      <ArrowDownIcon open={isOpen} />
                    </div>
                  </button>
                  {isOpen && (
                    <div className="px-3 pb-3 pt-1 border-t border-border bg-muted/10 text-xs space-y-2">
                      {(p.address_street || p.address_postcode || p.address_city) && (
                        <ExtField label="Address">
                          {p.address_street}<br />
                          {p.address_postcode} {p.address_city}
                          {p.address_country && <><br />{p.address_country}</>}
                        </ExtField>
                      )}
                      {p.iban    && <ExtField label="IBAN"><span className="font-mono">{p.iban}</span></ExtField>}
                      {p.tax_id  && <ExtField label="Tax-ID"><span className="font-mono">{p.tax_id}</span></ExtField>}
                      {p.emails?.length ? <ExtField label="Email"><span className="font-mono">{p.emails.join(", ")}</span></ExtField> : null}
                      {p.phones?.length ? <ExtField label="Phone"><span className="font-mono">{p.phones.join(", ")}</span></ExtField> : null}
                      {p.source_snippet && (
                        <ExtField label="Excerpt">
                          <pre className="text-[10px] bg-muted/40 rounded p-2 whitespace-pre-wrap font-mono leading-relaxed max-h-32 overflow-y-auto">
                            {p.source_snippet}
                          </pre>
                        </ExtField>
                      )}
                      {item.status === "pending" && (
                        <div className="flex gap-2 pt-2 border-t border-border">
                          {item.match_candidate_id ? (
                            <button
                              onClick={(e) => { e.stopPropagation(); decide(item, "accept_merge"); }}
                              disabled={isBusy}
                              className={cn(
                                "flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition",
                                "bg-amber-500 hover:bg-amber-600 text-white disabled:opacity-60",
                              )}
                            >
                              <ArrowRightLeft className="w-3 h-3" />
                              Merge into {item.match_display_name}
                            </button>
                          ) : null}
                          <button
                            onClick={(e) => { e.stopPropagation(); decide(item, "accept_create"); }}
                            disabled={isBusy || !(p.display_name || p.business_name)}
                            className={cn(
                              "flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition",
                              "bg-emerald-500 hover:bg-emerald-600 text-white disabled:opacity-50 disabled:cursor-not-allowed",
                            )}
                            title={!(p.display_name || p.business_name) ? "Proposal lacks a display name — reject and add manually" : undefined}
                          >
                            <UserPlus className="w-3 h-3" />
                            Create new
                          </button>
                          <button
                            onClick={(e) => { e.stopPropagation(); decide(item, "reject"); }}
                            disabled={isBusy}
                            className={cn(
                              "px-3 py-1.5 rounded-md text-xs transition",
                              "bg-muted/40 hover:bg-red-500/10 text-muted-foreground hover:text-red-500 disabled:opacity-60",
                            )}
                          >
                            <X className="w-3 h-3" />
                          </button>
                        </div>
                      )}
                      {item.status !== "pending" && item.created_contact_id && (
                        <div className="text-[10px] text-muted-foreground pt-1 border-t border-border">
                          → contact #{item.created_contact_id}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}


function ExtField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[80px_1fr] gap-x-3 text-[11px]">
      <div className="text-muted-foreground uppercase tracking-wider text-[10px] pt-0.5">{label}</div>
      <div>{children}</div>
    </div>
  );
}


function LogsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [data, setData] = useState<ErrorsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [level, setLevel] = useState<"" | "WARNING" | "ERROR" | "CRITICAL">("");
  const [limit, setLimit] = useState<number>(100);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const refresh = useCallback(async () => {
    setLoading(true);
    setForbidden(false);
    try {
      const qs = new URLSearchParams();
      qs.set("limit", String(limit));
      if (level) qs.set("level", level);
      const r = await api.get<ErrorsResponse>(`/api/system/errors?${qs.toString()}`);
      setData(r);
    } catch (e: any) {
      // The endpoint returns 403 for non-admin users — render a calm
      // explanation rather than a red error toast (it's expected for
      // member/child/viewer roles).
      const msg = String(e?.message || "");
      if (msg.includes("403") || /admin/i.test(msg)) {
        setForbidden(true);
      } else {
        toast(`Could not load logs: ${msg}`, "error");
      }
    } finally {
      setLoading(false);
    }
  }, [level, limit, toast]);

  useEffect(() => { refresh(); }, [refresh]);

  function toggle(id: number) {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function copyCorr(corr: string) {
    navigator.clipboard.writeText(corr).then(
      () => toast(`Copied ${corr}`, "success"),
      () => toast("Copy failed", "error"),
    );
  }

  function levelClasses(lvl: string): string {
    switch (lvl) {
      case "CRITICAL": return "bg-red-500/15 text-red-600 border-red-500/30";
      case "ERROR":    return "bg-red-500/10 text-red-500 border-red-500/20";
      case "WARNING":  return "bg-amber-500/10 text-amber-600 border-amber-500/20";
      default:         return "bg-muted text-muted-foreground border-border";
    }
  }

  return (
    <div>
      <header className="mb-6 flex items-baseline justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Logs</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Recent warnings, errors, and unhandled exceptions from the backend.
            For the full INFO-level stream, tail{" "}
            <code className="text-xs">data/logs/yorik.log</code> on the host.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={level}
            onChange={e => setLevel(e.target.value as typeof level)}
            className={cn(inputClass, "h-8 text-xs w-auto")}
          >
            <option value="">All levels</option>
            <option value="WARNING">Warning</option>
            <option value="ERROR">Error</option>
            <option value="CRITICAL">Critical</option>
          </select>
          <select
            value={limit}
            onChange={e => setLimit(Number(e.target.value))}
            className={cn(inputClass, "h-8 text-xs w-auto")}
          >
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={250}>250</option>
            <option value={500}>500</option>
          </select>
          <button
            onClick={refresh}
            className="w-8 h-8 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center justify-center"
            title="Reload"
          >
            <RefreshCw className={cn("w-3.5 h-3.5", loading && "animate-spin")} />
          </button>
        </div>
      </header>

      {forbidden && (
        <Card title="Admin only">
          <div className="flex items-start gap-3 py-2 text-sm">
            <Shield className="w-4 h-4 text-muted-foreground mt-0.5 shrink-0" />
            <div className="text-muted-foreground">
              The logs view is restricted to administrators because error
              messages can hint at infrastructure (LLM endpoint, IMAP host)
              we don't want to surface to lower-privilege roles.
            </div>
          </div>
        </Card>
      )}

      {!forbidden && !data && loading && (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
        </div>
      )}

      {!forbidden && data && (
        <div className="space-y-6">
          <Card title="By level">
            {Object.keys(data.summary).length === 0 ? (
              <EmptyMetric icon={Info} label="No warnings or errors recorded. Quiet is good." />
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                {(["CRITICAL", "ERROR", "WARNING"] as const).map(lvl => {
                  const n = data.summary[lvl] || 0;
                  if (n === 0) return null;
                  return (
                    <span
                      key={lvl}
                      className={cn(
                        "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs font-medium",
                        levelClasses(lvl),
                      )}
                    >
                      {lvl}
                      <span className="font-mono tabular-nums">{n}</span>
                    </span>
                  );
                })}
              </div>
            )}
          </Card>

          <Card title={`Recent ${level ? level.toLowerCase() : "entries"} (${data.errors.length})`}>
            {data.errors.length === 0 ? (
              <EmptyMetric icon={ScrollText} label="No entries matching this filter." />
            ) : (
              <div className="divide-y divide-border">
                {data.errors.map(row => {
                  const isOpen = expanded.has(row.id);
                  return (
                    <div key={row.id} className="py-2">
                      <button
                        onClick={() => toggle(row.id)}
                        className="w-full text-left flex items-start gap-3 group"
                      >
                        <ChevronRight
                          className={cn(
                            "w-3.5 h-3.5 mt-1 text-muted-foreground transition shrink-0",
                            isOpen && "rotate-90",
                          )}
                        />
                        <span
                          className={cn(
                            "inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider border shrink-0 mt-0.5",
                            levelClasses(row.level),
                          )}
                        >
                          {row.level}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-baseline gap-2 flex-wrap">
                            <span className="font-mono text-[11px] text-muted-foreground tabular-nums shrink-0">
                              {row.ts.replace("T", " ")}
                            </span>
                            <span className="font-mono text-[11px] text-muted-foreground truncate">
                              {row.logger}
                            </span>
                          </div>
                          <div className="text-sm mt-0.5 break-words">{row.message}</div>
                        </div>
                      </button>
                      {isOpen && (
                        <div className="mt-2 ml-7 space-y-2 text-xs">
                          {row.request_path && (
                            <div className="flex items-baseline gap-2">
                              <span className="text-muted-foreground shrink-0">path:</span>
                              <code className="font-mono break-all">{row.request_path}</code>
                            </div>
                          )}
                          {row.corr_id && (
                            <div className="flex items-center gap-2">
                              <span className="text-muted-foreground shrink-0">corr:</span>
                              <code className="font-mono">{row.corr_id}</code>
                              <button
                                onClick={() => copyCorr(row.corr_id!)}
                                title="Copy correlation id"
                                className="text-muted-foreground hover:text-foreground"
                              >
                                <Copy className="w-3 h-3" />
                              </button>
                              <span className="text-muted-foreground/70 text-[10px]">
                                {" — "}
                                <code className="font-mono">grep corr={row.corr_id} data/logs/yorik.log</code>
                              </span>
                            </div>
                          )}
                          {row.traceback && (
                            <pre className="bg-muted/40 border border-border rounded-md p-2 overflow-x-auto whitespace-pre text-[11px] leading-snug">
                              {row.traceback}
                            </pre>
                          )}
                          {!row.traceback && !row.corr_id && !row.request_path && (
                            <div className="text-muted-foreground italic">
                              No traceback or request context attached.
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}

// ─── shared bits ──────────────────────────────────────────────────────

const inputClass =
  "w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition";

function Card({ title, children }: { title: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <h3 className="text-xs uppercase tracking-wider font-semibold text-muted-foreground mb-3">{title}</h3>
      {children}
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

function EmptyMetric({ icon: Icon, label }:
  { icon: React.ComponentType<{ className?: string }>; label: string }) {
  return (
    <div className="text-center py-8 text-xs text-muted-foreground">
      <Icon className="w-7 h-7 mx-auto mb-2 opacity-30" />
      {label}
    </div>
  );
}

// ─── Spaces tab ─────────────────────────────────────────────────────
// Phase B.5: workspace kind + spaces management. Members add/remove
// fires the Paperless+Immich provisioning hooks server-side, so this
// UI also drives the bundled-service ACL sync.

interface SpaceMember {
  user_id: number;
  name: string;
  email: string;
  level: "read" | "write" | "admin";
  added_at: string;
  paperless_user_id: number | null;
  immich_user_id: string | null;
}
interface Space {
  id: number;
  name: string;
  kind: "personal" | "shared";
  slug: string | null;
  owner_user_id: number | null;
  members_count: number;
  your_level: "read" | "write" | "admin" | null;
}
interface SpaceDetail extends Space { members: SpaceMember[] }
interface UserRow { id: number; name: string; email: string; role: string }
interface Workspace { id: number; name: string; kind: "family" | "business"; owner_user_id: number }

function SpacesTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const auth = useAuth();
  const isAdmin = auth.user.role === "admin" || auth.user.role === "platform_admin";

  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [selectedSpaceId, setSelectedSpaceId] = useState<number | null>(null);
  const [detail, setDetail] = useState<SpaceDetail | null>(null);
  const [allUsers, setAllUsers] = useState<UserRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);

  const refreshSpaces = useCallback(async () => {
    setLoading(true);
    try {
      const [ws, ss] = await Promise.all([
        api.get<Workspace>("/api/workspaces/current"),
        api.get<Space[]>("/api/spaces"),
      ]);
      setWorkspace(ws);
      setSpaces(ss);
      if (selectedSpaceId === null && ss.length > 0) {
        const first = ss.find(s => s.kind === "shared") || ss[0];
        setSelectedSpaceId(first.id);
      }
    } catch (e: any) {
      toast(`Could not load spaces: ${e?.message || e}`, "error");
    } finally {
      setLoading(false);
    }
  }, [selectedSpaceId, toast]);

  useEffect(() => { void refreshSpaces(); }, [refreshSpaces]);

  useEffect(() => {
    if (selectedSpaceId === null) { setDetail(null); return; }
    (async () => {
      try {
        const d = await api.get<SpaceDetail>(`/api/spaces/${selectedSpaceId}`);
        setDetail(d);
      } catch (e: any) {
        toast(`Could not load space: ${e?.message || e}`, "error");
        setDetail(null);
      }
    })();
  }, [selectedSpaceId, toast]);

  // Lazy-load users only when admin opens the add-member picker.
  async function ensureUsers() {
    if (allUsers.length > 0) return;
    try { setAllUsers(await api.get<UserRow[]>("/api/users")); }
    catch (e: any) { toast(`Could not list users: ${e?.message || e}`, "error"); }
  }

  async function setWorkspaceKind(kind: "family" | "business") {
    if (!isAdmin || !workspace) return;
    try {
      const next = await api.patch<Workspace>("/api/workspaces/current", { kind });
      setWorkspace(next);
      toast(`Workspace kind set to ${kind}.`, "success");
    } catch (e: any) {
      toast(`Could not update workspace: ${e?.message || e}`, "error");
    }
  }

  async function createSpace(name: string, slug: string) {
    if (!isAdmin) return;
    try {
      const d = await api.post<SpaceDetail>("/api/spaces", { name, slug: slug || undefined });
      await refreshSpaces();
      setSelectedSpaceId(d.id);
      setCreating(false);
      toast(`Created space "${d.name}".`, "success");
    } catch (e: any) {
      toast(`Could not create space: ${e?.message || e}`, "error");
    }
  }

  async function deleteSpace(s: Space) {
    if (!isAdmin) return;
    if (!confirm(`Delete space "${s.name}"? This can't be undone.`)) return;
    try {
      await api.delete(`/api/spaces/${s.id}`);
      setSelectedSpaceId(null);
      await refreshSpaces();
      toast(`Deleted "${s.name}".`, "success");
    } catch (e: any) {
      toast(`Could not delete: ${e?.message || e}`, "error");
    }
  }

  async function addMember(user_id: number, level: "read" | "write" | "admin") {
    if (!isAdmin || !detail) return;
    try {
      const d = await api.post<SpaceDetail>(`/api/spaces/${detail.id}/members`,
        { user_id, level });
      setDetail(d);
      await refreshSpaces();
      toast(`Added member.`, "success");
    } catch (e: any) {
      toast(`Could not add member: ${e?.message || e}`, "error");
    }
  }

  async function patchMemberLevel(user_id: number, level: "read" | "write" | "admin") {
    if (!isAdmin || !detail) return;
    try {
      const d = await api.patch<SpaceDetail>(
        `/api/spaces/${detail.id}/members/${user_id}`, { level });
      setDetail(d);
    } catch (e: any) {
      toast(`Could not update level: ${e?.message || e}`, "error");
    }
  }

  async function removeMember(user_id: number) {
    if (!isAdmin || !detail) return;
    try {
      await api.delete(`/api/spaces/${detail.id}/members/${user_id}`);
      const d = await api.get<SpaceDetail>(`/api/spaces/${detail.id}`);
      setDetail(d);
      await refreshSpaces();
    } catch (e: any) {
      toast(`Could not remove: ${e?.message || e}`, "error");
    }
  }

  if (loading && !workspace) {
    return <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="w-4 h-4 animate-spin" /> Loading spaces…</div>;
  }

  return (
    <div className="space-y-6">
      {/* Workspace kind */}
      {workspace && (
        <div className="rounded-2xl border border-border bg-card p-5">
          <div className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">Workspace</div>
          <div className="flex items-center gap-3">
            <div className="flex-1 min-w-0">
              <div className="font-medium text-base truncate">{workspace.name}</div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Kind picks default placement for new events / tasks / contacts. You can change a row's space anytime.
              </div>
            </div>
            <div className="flex gap-1 shrink-0">
              {(["family", "business"] as const).map(k => (
                <button
                  key={k}
                  onClick={() => setWorkspaceKind(k)}
                  disabled={!isAdmin || workspace.kind === k}
                  className={cn(
                    "px-3 py-1.5 rounded-md text-sm font-medium transition border",
                    workspace.kind === k
                      ? "bg-primary text-primary-foreground border-primary"
                      : "border-border hover:bg-muted",
                    !isAdmin && "opacity-50 cursor-not-allowed",
                  )}
                  title={!isAdmin ? "Admin only" : ""}
                >{k}</button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Spaces list + detail */}
      <div className="grid md:grid-cols-[280px_1fr] gap-4">
        <div className="space-y-1">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold">Spaces</div>
            {isAdmin && (
              <button
                onClick={() => setCreating(true)}
                className="text-xs text-blue-600 dark:text-blue-300 hover:underline"
              >+ New</button>
            )}
          </div>
          {spaces.map(s => (
            <button
              key={s.id}
              onClick={() => setSelectedSpaceId(s.id)}
              className={cn(
                "w-full text-left px-3 py-2 rounded-lg border transition flex items-center gap-2",
                selectedSpaceId === s.id
                  ? "border-primary/40 bg-primary/5"
                  : "border-border hover:bg-muted/40",
              )}
            >
              <span className={cn(
                "w-1.5 h-1.5 rounded-full",
                s.kind === "shared" ? "bg-teal-500" : "bg-violet-400",
              )} />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium truncate">{s.name}</div>
                <div className="text-[10px] text-muted-foreground">
                  {s.kind === "shared" ? `${s.members_count} member${s.members_count === 1 ? "" : "s"}` : "personal"}
                </div>
              </div>
            </button>
          ))}
        </div>

        <div>
          {creating ? (
            <CreateSpaceForm onCancel={() => setCreating(false)} onCreate={createSpace} />
          ) : detail ? (
            <SpaceDetailPanel
              detail={detail}
              isAdmin={isAdmin}
              allUsers={allUsers}
              ensureUsers={ensureUsers}
              onAddMember={addMember}
              onPatchLevel={patchMemberLevel}
              onRemoveMember={removeMember}
              onDelete={() => deleteSpace(detail)}
            />
          ) : (
            <div className="text-sm text-muted-foreground italic">Pick a space on the left.</div>
          )}
        </div>
      </div>
    </div>
  );
}

function CreateSpaceForm({ onCancel, onCreate }: { onCancel: () => void; onCreate: (n: string, s: string) => void }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  return (
    <div className="rounded-2xl border border-border bg-card p-5">
      <div className="text-[11px] uppercase tracking-wider text-muted-foreground font-semibold mb-3">New shared space</div>
      <div className="space-y-3">
        <div>
          <label className="text-xs text-muted-foreground">Name</label>
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="Customers"
            className="mt-1 w-full px-3 py-2 rounded-lg border border-border bg-background text-sm"
            autoFocus
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Slug (lowercase, optional)</label>
          <input
            value={slug}
            onChange={e => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))}
            placeholder="customers"
            className="mt-1 w-full px-3 py-2 rounded-lg border border-border bg-background text-sm font-mono"
          />
          <div className="text-[10px] text-muted-foreground mt-1">
            Used by Paperless + Immich for the matching group / album name.
          </div>
        </div>
        <div className="flex gap-2 pt-1">
          <button
            onClick={() => onCreate(name.trim(), slug.trim())}
            disabled={!name.trim()}
            className="px-4 py-1.5 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
          >Create</button>
          <button
            onClick={onCancel}
            className="px-4 py-1.5 rounded-md border border-border text-sm hover:bg-muted"
          >Cancel</button>
        </div>
      </div>
    </div>
  );
}

function SpaceDetailPanel({
  detail, isAdmin, allUsers, ensureUsers,
  onAddMember, onPatchLevel, onRemoveMember, onDelete,
}: {
  detail: SpaceDetail;
  isAdmin: boolean;
  allUsers: UserRow[];
  ensureUsers: () => Promise<void>;
  onAddMember: (uid: number, level: "read" | "write" | "admin") => void;
  onPatchLevel: (uid: number, level: "read" | "write" | "admin") => void;
  onRemoveMember: (uid: number) => void;
  onDelete: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [pickUserId, setPickUserId] = useState<number | "">("");
  const [pickLevel, setPickLevel] = useState<"read" | "write" | "admin">("write");

  const isPersonal = detail.kind === "personal";
  const memberIds = new Set(detail.members.map(m => m.user_id));
  const availableUsers = allUsers.filter(u => !memberIds.has(u.id));

  return (
    <div className="rounded-2xl border border-border bg-card p-5 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-base font-semibold">{detail.name}</div>
          <div className="text-[11px] text-muted-foreground mt-0.5">
            {isPersonal ? "Personal space — visible only to its owner." :
              `Shared space${detail.slug ? ` · slug "${detail.slug}"` : ""} · ${detail.members.length} member${detail.members.length === 1 ? "" : "s"}`}
          </div>
        </div>
        {isAdmin && !isPersonal && detail.slug !== "household" && detail.slug !== "finance" && (
          <button
            onClick={onDelete}
            className="text-xs text-rose-600 dark:text-rose-400 hover:underline shrink-0"
          >Delete space</button>
        )}
      </div>

      <div className="space-y-2">
        {detail.members.map(m => (
          <div key={m.user_id} className="flex items-center gap-3 px-3 py-2 rounded-lg border border-border">
            <div className="w-8 h-8 rounded-full bg-muted/60 flex items-center justify-center text-sm font-semibold">
              {m.name.slice(0, 1).toUpperCase()}
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium truncate">{m.name}</div>
              <div className="text-[11px] text-muted-foreground truncate">
                {m.email}
                {(m.paperless_user_id || m.immich_user_id) && (
                  <span className="ml-2 text-[10px] uppercase tracking-wider">
                    {m.paperless_user_id ? "· paperless ✓" : ""}{m.immich_user_id ? " · immich ✓" : ""}
                  </span>
                )}
                {!m.paperless_user_id && !m.immich_user_id && (
                  <span className="ml-2 text-[10px] uppercase tracking-wider text-amber-600 dark:text-amber-400">
                    · no external accounts linked
                  </span>
                )}
              </div>
            </div>
            {isAdmin && !isPersonal ? (
              <>
                <select
                  value={m.level}
                  onChange={e => onPatchLevel(m.user_id, e.target.value as any)}
                  className="text-xs px-2 py-1 rounded border border-border bg-background"
                >
                  <option value="read">read</option>
                  <option value="write">write</option>
                  <option value="admin">admin</option>
                </select>
                <button
                  onClick={() => onRemoveMember(m.user_id)}
                  className="text-rose-600 dark:text-rose-400 hover:bg-rose-500/10 p-1.5 rounded"
                  title="Remove member"
                >
                  <X className="w-4 h-4" />
                </button>
              </>
            ) : (
              <span className="text-xs text-muted-foreground uppercase tracking-wider">
                {m.level}
              </span>
            )}
          </div>
        ))}
        {detail.members.length === 0 && (
          <div className="text-sm text-muted-foreground italic px-3 py-2">No members yet.</div>
        )}
      </div>

      {isAdmin && !isPersonal && (
        <div className="pt-2 border-t border-border">
          {!adding ? (
            <button
              onClick={async () => { await ensureUsers(); setAdding(true); }}
              className="text-sm text-blue-600 dark:text-blue-300 hover:underline inline-flex items-center gap-1"
            >
              <UserPlus className="w-4 h-4" /> Add member
            </button>
          ) : (
            <div className="flex items-center gap-2">
              <select
                value={pickUserId}
                onChange={e => setPickUserId(e.target.value ? parseInt(e.target.value, 10) : "")}
                className="flex-1 px-2 py-1.5 rounded border border-border bg-background text-sm"
              >
                <option value="">— pick user —</option>
                {availableUsers.map(u => (
                  <option key={u.id} value={u.id}>{u.name} ({u.email})</option>
                ))}
              </select>
              <select
                value={pickLevel}
                onChange={e => setPickLevel(e.target.value as any)}
                className="px-2 py-1.5 rounded border border-border bg-background text-sm"
              >
                <option value="read">read</option>
                <option value="write">write</option>
                <option value="admin">admin</option>
              </select>
              <button
                disabled={!pickUserId}
                onClick={() => {
                  if (!pickUserId) return;
                  onAddMember(pickUserId, pickLevel);
                  setAdding(false);
                  setPickUserId("");
                  setPickLevel("write");
                }}
                className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
              >Add</button>
              <button
                onClick={() => { setAdding(false); setPickUserId(""); }}
                className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted"
              >Cancel</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}


// ─── Installed Apps (v2 platform) ──────────────────────────────────────
//
// Lists active manifest_version: 2 installs from the installed_apps
// ledger. Shows the granted scopes the user agreed to at install, the
// app's owned schema, and an Uninstall action. Includes a small
// "Install from path" form that opens the AppInstallConsentDialog —
// the dev/admin install path that doesn't go through the marketplace
// catalog.

interface InstalledV2App {
  app_id: string;
  owned_schema: string;
  manifest: any;
  granted_permissions: any;
  source_dir: string | null;
}

function InstalledAppsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const [apps, setApps] = useState<InstalledV2App[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [installPath, setInstallPath] = useState("");
  const [consentSourceDir, setConsentSourceDir] = useState<string | null>(null);
  const [pendingUninstall, setPendingUninstall] = useState<InstalledV2App | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await api.get<InstalledV2App[]>("/api/apps/installed-v2");
      setApps(Array.isArray(list) ? list : []);
    } catch (e: any) {
      setApps([]);
      if (e?.status && e.status !== 403) toast("Could not load installed apps", "error");
    }
  }, [toast]);

  useEffect(() => { void load(); }, [load]);

  async function doUninstall(app: InstalledV2App) {
    setPendingUninstall(null);
    setBusy(app.app_id);
    try {
      await api.delete(`/api/apps/${app.app_id}?wipe_data=true`);
      toast(`${app.manifest?.name || app.app_id} uninstalled`, "success");
      await load();
    } catch {
      toast(`Failed to uninstall ${app.app_id}`, "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">Installed apps</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Community apps installed against the Phase E platform — each one
          owns a Postgres schema and a per-app role scoped to its own data.
        </p>
      </header>

      {/* Install-from-path admin form */}
      <div className="mb-5 rounded-xl border border-border bg-card p-4">
        <div className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground mb-2">
          Install from source directory
        </div>
        <div className="flex gap-2">
          <input
            type="text"
            value={installPath}
            onChange={(e) => setInstallPath(e.target.value)}
            placeholder="/abs/path/to/app/source"
            className="flex-1 px-3 py-2 rounded-md border border-border bg-background text-sm font-mono"
          />
          <button
            onClick={() => installPath && setConsentSourceDir(installPath)}
            disabled={!installPath}
            className={cn(
              "px-3 py-2 rounded-md text-sm font-medium bg-primary text-primary-foreground",
              "hover:bg-primary/90 transition inline-flex items-center gap-1.5",
              !installPath && "opacity-50 cursor-not-allowed",
            )}
          >
            <Plus className="w-3.5 h-3.5" />
            Preview & install
          </button>
        </div>
        <p className="text-xs text-muted-foreground mt-2 leading-relaxed">
          Opens the consent dialog with the parsed manifest. Use this for dev
          installs from outside the marketplace catalog.
        </p>
      </div>

      {apps === null && (
        <div className="text-sm text-muted-foreground">Loading…</div>
      )}

      {apps && apps.length === 0 && (
        <div className="bg-card border border-border rounded-xl p-8 text-sm text-muted-foreground text-center">
          <Package className="w-6 h-6 mx-auto mb-2 opacity-50" />
          <div>No v2 community apps installed.</div>
          <div className="mt-1 text-xs">
            Install one from the Marketplace tab, or paste a source directory above.
          </div>
        </div>
      )}

      {apps && apps.length > 0 && (
        <div className="space-y-3">
          {apps.map(a => (
            <InstalledAppCard
              key={a.app_id}
              app={a}
              busy={busy === a.app_id}
              onUninstall={() => setPendingUninstall(a)}
            />
          ))}
        </div>
      )}

      {consentSourceDir && (
        <AppInstallConsentDialog
          sourceDir={consentSourceDir}
          onClose={() => setConsentSourceDir(null)}
          onInstalled={(appId) => {
            toast(`${appId} installed`, "success");
            void load();
            setInstallPath("");
          }}
        />
      )}

      {pendingUninstall && (
        <InstalledAppUninstallConfirmModal
          app={pendingUninstall}
          onClose={() => setPendingUninstall(null)}
          onConfirm={() => doUninstall(pendingUninstall)}
        />
      )}
    </div>
  );
}

function InstalledAppUninstallConfirmModal({ app, onClose, onConfirm }: {
  app: InstalledV2App;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const name = app.manifest?.name || app.app_id;
  return createPortal(
    <div
      className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm bg-card border border-border rounded-2xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="font-semibold">Uninstall {name}?</div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground" aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-4">
          <div className="text-sm text-muted-foreground leading-relaxed">
            This removes <span className="font-mono text-[12px]">{app.app_id}</span> and
            <strong className="text-foreground"> wipes its Postgres schema</strong>
            {" "}<span className="font-mono text-[11px]">{app.owned_schema}</span>
            {" "}plus the data dir at
            <span className="font-mono text-[11px]"> data/apps/{app.app_id}/</span>.
          </div>
          <div className="text-[12px] text-muted-foreground">
            Reinstall is possible from the Marketplace, but the data does not come back.
          </div>
          <div className="flex gap-2 pt-2">
            <button
              onClick={onClose}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium border border-border hover:bg-muted transition"
            >
              Keep installed
            </button>
            <button
              onClick={onConfirm}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium bg-destructive text-white hover:opacity-90 transition"
            >
              Uninstall and wipe
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

function InstalledAppCard({
  app, busy, onUninstall,
}: { app: InstalledV2App; busy: boolean; onUninstall: () => void }) {
  const perms = app.granted_permissions || {};
  const reads = perms.reads || [];
  const skills = perms.invokes_skills || [];
  const realtime = perms.realtime_subscriptions || [];
  const ownedTables = app.manifest?.owned_tables || [];

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <div className="flex items-start gap-4">
        <div className="text-2xl shrink-0">{app.manifest?.icon || "📦"}</div>
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2 flex-wrap">
            <div className="font-semibold">{app.manifest?.name || app.app_id}</div>
            <span className="text-xs text-muted-foreground">
              v{app.manifest?.version}
            </span>
            <span className="text-[10px] uppercase tracking-wider font-mono text-muted-foreground">
              {app.app_id}
            </span>
          </div>
          {app.manifest?.description && (
            <p className="text-sm text-muted-foreground mt-1 leading-relaxed">
              {app.manifest.description}
            </p>
          )}
        </div>
        <button
          onClick={onUninstall}
          disabled={busy}
          className={cn(
            "shrink-0 px-3 py-1.5 rounded-md text-sm font-medium border border-border",
            "hover:bg-muted text-muted-foreground hover:text-foreground transition",
            busy && "opacity-50 cursor-wait",
          )}
        >
          {busy ? "Uninstalling…" : "Uninstall"}
        </button>
      </div>

      <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
        <PermFact
          label="Owned schema"
          value={
            <span className="font-mono text-[11px]">{app.owned_schema}</span>
          }
        />
        <PermFact
          label="Tables"
          value={
            ownedTables.length > 0 ? (
              <span className="font-mono text-[11px]">{ownedTables.join(", ")}</span>
            ) : <span className="text-muted-foreground">—</span>
          }
        />
        <PermFact
          label={`Reads (${reads.length})`}
          value={
            reads.length > 0 ? (
              <span className="text-foreground">
                {reads.map((r: any) => r.table).join(", ")}
              </span>
            ) : <span className="text-muted-foreground">none</span>
          }
        />
        <PermFact
          label={`Skills (${skills.length})`}
          value={
            skills.length > 0 ? (
              <span className="text-foreground">{skills.join(", ")}</span>
            ) : <span className="text-muted-foreground">none</span>
          }
        />
        {realtime.length > 0 && (
          <PermFact
            label="Realtime"
            value={
              <span className="font-mono text-[11px]">{realtime.join(", ")}</span>
            }
          />
        )}
        {app.source_dir && (
          <PermFact
            label="Source"
            value={
              <span className="font-mono text-[10px] text-muted-foreground break-all">
                {app.source_dir}
              </span>
            }
          />
        )}
      </div>
    </div>
  );
}

function PermFact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground mb-0.5">
        {label}
      </div>
      <div>{value}</div>
    </div>
  );
}
