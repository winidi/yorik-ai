/**
 * Settings — the hub. Three groups (settings audit 2026-10-04):
 *   - You: Profile, Sign-in & devices, Privacy & safety, Assistant & alerts
 *   - Household (admin, Letters for every adult): People, Apps &
 *     accounts, Letters & invoices, Backup & storage
 *   - Maintenance (admin, folded): Health & updates, AI, Developer,
 *     Hosted families (owner)
 * plus "Start here" for the admin. The pages below are made of the
 * cards that used to be tabs of their own.
 *
 * Three-pane shell to match the rest of the React apps.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import i18n, { locale } from "@/i18n";
import {
  Loader2, Settings as Cog, User as UserIcon, Hash, BarChart3,
  Plug, ExternalLink, Save, CheckCircle2, AlertCircle, Sparkles,
  RefreshCw, ThumbsUp, ThumbsDown, FileText, Lightbulb, Database,
  Menu, Cpu, Wifi, Search, Sun, Moon, Monitor, Mic, Grid3x3, MonitorSmartphone, Square,
  Users, UsersRound, UserPlus, KeyRound, Trash2, Power, Copy, X,
  HardDrive, ArrowRightLeft, AlertTriangle, Shield, Upload,
  Tag as TagIcon, XCircle, Info, ScrollText, ChevronRight, Puzzle,
  Store, Download, BadgeCheck, Lock, Globe, Package, Plus, Home, Activity, QrCode,
} from "lucide-react";
import { SystemStatusPanel } from "@/components/SystemStatusPanel";
import { isKid } from "@/lib/dock-order";
import { openHelp } from "@/components/HelpPanel";
import { InviteDialog } from "@/components/InviteDialog";
import { useTriPane, MobileBackdrop, mobileAsideLeft } from "@/components/MobileShell";
import { AppInstallConsentDialog } from "@/components/AppInstallConsentDialog";
import { DiagnosticsCard } from "@/components/DiagnosticsConsent";
import { DiagnosticsSection } from "@/components/DiagnosticsReports";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useAuth } from "@/components/AuthGate";
import { Dock } from "@/components/Dock";
import { StoragePicker } from "@/components/StoragePicker";
import { BackupPicker } from "@/components/BackupPicker";
import { EasyBackup } from "@/components/EasyBackup";
import { SeriesManager } from "@/apps/compose/SeriesManager";
import { DevicesTab } from "./DevicesTab";
import { HouseholdsTab } from "./HouseholdsTab";
import { StartHereTab } from "./StartHereTab";
import { ConnectionsCard } from "./ConnectionsCard";
import { PushCard } from "@/components/PushCard";
import { AgentCard } from "@/components/AgentCard";
import { PlanningRulesCard } from "@/components/PlanningRulesCard";
import { ProfileLookCard } from "@/components/ProfileLookCard";
import { SharingCard } from "@/components/SharingCard";
import { LetterheadCard } from "@/components/LetterheadCard";

type Tab = "start" | "profile" | "signin" | "privacy" | "assistant" | "people" | "apps" | "letters" | "backup" | "system" | "ai" | "developer" | "households";
type Group = "start" | "you" | "household" | "maintenance";

// The sidebar in three groups a newcomer can place (settings audit,
// 2026-10-04): You (my own things), Household (what the admin sets up
// for everyone), Maintenance (the technical side, folded away). Start
// here leads a new admin through the rest.
//   adminOnly  = admins only (admin, platform_admin)
//   ownerOnly  = platform_admin only (the install's owner)
//   hostOnly   = hidden on a tenant Yorik (state shared by all tenants)
//   adults     = hidden for children
//   kids       = shown to children too
const TABS: { id: Tab; label: string; group: Group; icon: React.ComponentType<{ className?: string }>; color: string;
              adminOnly?: boolean; ownerOnly?: boolean; hostOnly?: boolean; adults?: boolean; kids?: boolean }[] = [
  { id: "start",      label: "settings.tabs.start",      group: "start",       icon: Sparkles,  color: "text-violet-500 bg-violet-500/10", adminOnly: true },
  { id: "profile",    label: "settings.tabs.profile",    group: "you",         icon: UserIcon,  color: "text-violet-500 bg-violet-500/10", kids: true },
  { id: "signin",     label: "settings.tabs.signin",     group: "you",         icon: KeyRound,  color: "text-blue-500 bg-blue-500/10", kids: true },
  { id: "privacy",    label: "settings.tabs.privacy",    group: "you",         icon: Shield,    color: "text-teal-500 bg-teal-500/10", adults: true },
  { id: "assistant",  label: "settings.tabs.assistant",  group: "you",         icon: Lightbulb, color: "text-yellow-500 bg-yellow-500/10", adults: true },
  { id: "people",     label: "settings.tabs.people",     group: "household",   icon: Users,     color: "text-cyan-500 bg-cyan-500/10", adminOnly: true },
  { id: "apps",       label: "settings.tabs.apps",       group: "household",   icon: Grid3x3,   color: "text-fuchsia-500 bg-fuchsia-500/10", adminOnly: true },
  { id: "letters",    label: "settings.tabs.letters",    group: "household",   icon: FileText,  color: "text-rose-500 bg-rose-500/10", adults: true },
  { id: "backup",     label: "settings.tabs.backup",     group: "household",   icon: HardDrive, color: "text-emerald-500 bg-emerald-500/10", adminOnly: true, hostOnly: true },
  { id: "system",     label: "settings.tabs.system",     group: "maintenance", icon: Activity,  color: "text-emerald-500 bg-emerald-500/10", adminOnly: true, hostOnly: true },
  { id: "ai",         label: "settings.tabs.ai",         group: "maintenance", icon: Cpu,       color: "text-blue-500 bg-blue-500/10", adminOnly: true, hostOnly: true },
  { id: "developer",  label: "settings.tabs.developer",  group: "maintenance", icon: ScrollText,color: "text-orange-500 bg-orange-500/10", adminOnly: true },
  { id: "households", label: "settings.tabs.households", group: "maintenance", icon: Home,      color: "text-orange-500 bg-orange-500/10", ownerOnly: true, hostOnly: true },
];

const GROUP_LABEL: Record<Group, string> = { start: "", you: "settings.tabs.groups.you", household: "settings.tabs.groups.household", maintenance: "settings.tabs.groups.maintenance" };

// Old tab names (bookmarks, links in help pages and other apps) → the
// page that holds that setting now; a few open a section inside it.
const LEGACY_TAB: Record<string, { tab: Tab; section?: string }> = {
  devices: { tab: "signin" }, llm: { tab: "ai" }, embeddings: { tab: "ai" },
  users: { tab: "people" }, spaces: { tab: "people" }, connectors: { tab: "apps" },
  numbering: { tab: "letters" }, extensions: { tab: "letters" }, storage: { tab: "backup" },
  logs: { tab: "developer", section: "logs" }, quality: { tab: "developer", section: "quality" },
  skills: { tab: "developer", section: "skills" }, marketplace: { tab: "developer", section: "addons" },
  installed: { tab: "developer", section: "addons" },
};

function resolveTab(want: string | null): { tab: Tab | null; section?: string } {
  if (!want) return { tab: null };
  if (TABS.some(t => t.id === want)) return { tab: want as Tab };
  return LEGACY_TAB[want] || { tab: null };
}

export function SettingsApp() {
  const { t: tr } = useTranslation();
  const auth = useAuth();
  const role = auth.user.role;
  const isAdmin = role === "admin" || role === "platform_admin";
  const kid = isKid(role);
  const visibleTabs = TABS.filter(t =>
    (!t.adminOnly || isAdmin) && (!t.ownerOnly || role === "platform_admin") &&
    (!auth.isTenant || !t.hostOnly) && (!kid || t.kids) && (!t.adults || !kid)
  );
  // The tab lives in the URL (?tab=…), so a reload, the back button and
  // links from Home or other apps land on the right page.
  const [params, setParams] = useSearchParams();
  const wanted = resolveTab(params.get("tab"));
  const fallback: Tab = isAdmin && !auth.isTenant ? "start" : "profile";
  const activeTab: Tab = wanted.tab && visibleTabs.some(t => t.id === wanted.tab) ? wanted.tab
    : visibleTabs.some(t => t.id === fallback) ? fallback : "profile";
  const section = wanted.section || params.get("section") || undefined;
  const setTab = useCallback((id: Tab, sec?: string) => {
    setParams(sec ? { tab: id, section: sec } : { tab: id }, { replace: false });
  }, [setParams]);
  const [showMaintenance, setShowMaintenance] = useState<boolean>(() => {
    try { return localStorage.getItem("yorik_settings_advanced") === "1"; } catch { return false; }
  });
  const maintenanceOpen = showMaintenance || TABS.find(t => t.id === activeTab)?.group === "maintenance";
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

  const tabButton = (t: typeof TABS[number]) => (
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
      <span className="text-sm font-medium">{tr(t.label)}</span>
    </button>
  );
  const groupTabs = (g: Group) => visibleTabs.filter(t => t.group === g);

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
            <div className="font-semibold leading-none">{tr("settings.shell.title")}</div>
            <div className="text-2xs text-muted-foreground mt-0.5">
              {auth.user.name}
            </div>
          </div>
        </header>

        <nav className="flex-1 overflow-y-auto p-2 space-y-1">
          {groupTabs("start").map(tabButton)}
          {(["you", "household"] as Group[]).map(g => groupTabs(g).length > 0 && (
            <div key={g} className="pt-2 space-y-1">
              <div className="px-3 pt-1 pb-1 text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
                {tr(GROUP_LABEL[g])}
              </div>
              {groupTabs(g).map(tabButton)}
            </div>
          ))}
          {groupTabs("maintenance").length > 0 && (
            <div className="pt-2 space-y-1">
              <button
                onClick={() => setShowMaintenance(v => { try { localStorage.setItem("yorik_settings_advanced", v ? "0" : "1"); } catch {} return !v; })}
                className="w-full text-left px-3 pt-1 pb-1 text-2xs font-semibold uppercase tracking-wide text-muted-foreground hover:text-foreground flex items-center gap-1"
                aria-expanded={maintenanceOpen}
              >
                <ChevronRight className={cn("w-3 h-3 transition", maintenanceOpen && "rotate-90")} /> {tr(GROUP_LABEL.maintenance)}
              </button>
              {maintenanceOpen && groupTabs("maintenance").map(tabButton)}
            </div>
          )}
        </nav>

        <footer className="border-t border-border px-4 py-3 text-xs space-y-2">
          <button
            onClick={() => openHelp()}
            className="w-full text-left text-muted-foreground hover:text-foreground transition"
          >
            {tr("settings.shell.help")}
          </button>
          <button
            onClick={() => auth.logout()}
            className="w-full text-left text-muted-foreground hover:text-foreground transition"
          >
            {tr("settings.shell.signOut")}
          </button>
        </footer>
      </aside>

      <section className="flex-1 overflow-y-auto bg-background min-w-0">
        <div className="md:hidden h-12 px-3 border-b border-border bg-background/85 backdrop-blur flex items-center gap-2 sticky top-0 z-30">
          <button
            onClick={() => tri.setLeftOpen(true)}
            aria-label={tr("settings.shell.openMenu")}
            className="w-9 h-9 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center justify-center"
          >
            <Menu className="w-4 h-4" />
          </button>
          <div className="text-sm font-medium truncate flex-1">
            {(() => { const cur = visibleTabs.find(x => x.id === activeTab); return cur ? tr(cur.label) : ""; })()}
          </div>
        </div>
        <div className="max-w-3xl mx-auto px-4 sm:px-8 py-6 sm:py-8">
          {activeTab === "start"      && <StartHereTab onOpen={setTab} available={visibleTabs.map(t => t.id)} />}
          {activeTab === "profile"    && <ProfileTab toast={toast} />}
          {activeTab === "signin"     && <SignInPage toast={toast} />}
          {activeTab === "privacy"    && <PrivacyPage toast={toast} />}
          {activeTab === "assistant"  && <AssistantPage toast={toast} />}
          {activeTab === "people"     && <PeoplePage toast={toast} openGroups={section === "groups"} />}
          {activeTab === "apps"       && <AppsAccountsPage toast={toast} />}
          {activeTab === "letters"    && <LettersPage toast={toast} isAdmin={isAdmin && !auth.isTenant} />}
          {activeTab === "backup"     && <BackupStoragePage toast={toast} />}
          {activeTab === "system"     && <SystemStatusPanel />}
          {activeTab === "ai"         && <AiPage toast={toast} />}
          {activeTab === "developer"  && <DeveloperPage toast={toast} section={section} onSection={sec => setTab("developer", sec)} hostOnlyHidden={auth.isTenant} />}
          {activeTab === "households" && <HouseholdsTab toast={toast} />}
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

// ─── Pages ────────────────────────────────────────────────────────────
// Each page is a short header plus the cards that belong to it.

type ToastFn = (text: string, kind?: "info" | "success" | "error") => void;

function PageHeader({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <header className="mb-6">
      <h1 className="text-2xl font-semibold">{title}</h1>
      {children && <p className="text-sm text-muted-foreground mt-1">{children}</p>}
    </header>
  );
}

/** A folded section for the rarely needed parts of a page. */
function MoreSection({ title, hint, open, children }: { title: string; hint?: string; open?: boolean; children: React.ReactNode }) {
  return (
    <details className="group rounded-xl border border-border bg-card/40" open={open || undefined}>
      <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium flex items-center gap-2">
        <ChevronRight className="w-4 h-4 transition group-open:rotate-90" />
        {title}
        {hint && <span className="text-xs font-normal text-muted-foreground">{hint}</span>}
      </summary>
      <div className="px-4 pb-4 space-y-5">{children}</div>
    </details>
  );
}

function SignInPage({ toast }: { toast: ToastFn }) {
  const { t } = useTranslation();
  return (
    <div>
      <PageHeader title={t("settings.pages.signin.title")}>
        {t("settings.pages.signin.intro")}
      </PageHeader>
      <div className="space-y-5">
        <ChangePasswordCard toast={toast} />
        <KioskPinCard toast={toast} />
        <VoiceEnrollmentCard toast={toast} />
        <DevicesTab toast={toast} />
      </div>
    </div>
  );
}

function PrivacyPage({ toast }: { toast: ToastFn }) {
  const { t } = useTranslation();
  return (
    <div>
      <PageHeader title={t("settings.pages.privacy.title")}>
        {t("settings.pages.privacy.intro")}
      </PageHeader>
      <div className="space-y-5">
        <SharingCard toast={toast} />
        <DefaultDocVisibilityChips toast={toast} />
        <KioskAgendaConsentCard toast={toast} />
        <KioskPhotosConsentCard toast={toast} />
        <EmergencyAccessCard toast={toast} />
        <DiagnosticsCard toast={toast} onOpenReports={() => { window.location.assign("/r/settings?tab=developer&section=diagnostics"); }} />
      </div>
    </div>
  );
}

function AssistantPage({ toast }: { toast: ToastFn }) {
  const { t } = useTranslation();
  return (
    <div>
      <PageHeader title={t("settings.pages.assistant.title")}>
        {t("settings.pages.assistant.intro")}
      </PageHeader>
      <div className="space-y-5">
        <PushCard toast={toast} />
        <ConfirmMutationsToggle toast={toast} part="undo" />
        <SuggestionEngineCard toast={toast} />
        <PlanningRulesCard toast={toast} />
        <Card title={t("settings.pages.assistant.voice")}>
          <VoiceAckToggle toast={toast} />
        </Card>
        <MoreSection title={t("settings.pages.assistant.ownAgent")} hint={t("settings.pages.assistant.ownAgentHint")}>
          <AgentCard toast={toast} />
          <ApiTokensCard toast={toast} />
          <ConfirmMutationsToggle toast={toast} part="agents" />
          <DevModeToggle toast={toast} />
        </MoreSection>
      </div>
    </div>
  );
}

function PeoplePage({ toast, openGroups }: { toast: ToastFn; openGroups?: boolean }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-6">
      <UsersTab toast={toast} />
      <MoreSection title={t("settings.pages.people.groups")} hint={t("settings.pages.people.groupsHint")} open={openGroups}>
        <SpacesTab toast={toast} />
      </MoreSection>
    </div>
  );
}

function AppsAccountsPage({ toast }: { toast: ToastFn }) {
  return (
    <div className="space-y-8">
      <AppsTab toast={toast} />
      <ConnectionsCard />
      <MapsConnectorCard />
    </div>
  );
}

function LettersPage({ toast, isAdmin }: { toast: ToastFn; isAdmin: boolean }) {
  const { t } = useTranslation();
  return (
    <div>
      <PageHeader title={t("settings.pages.letters.title")}>
        {t("settings.pages.letters.intro")}
      </PageHeader>
      <div className="space-y-5">
        <BusinessCard toast={toast} />
        <SignatureUpload toast={toast} />
        <LetterheadCard toast={toast} />
        <NumberingTab toast={toast} />
        {isAdmin && <ExtensionsTab toast={toast} />}
      </div>
    </div>
  );
}

function BackupStoragePage({ toast }: { toast: ToastFn }) {
  const { t } = useTranslation();
  return (
    <div>
      <PageHeader title={t("settings.pages.backup.title")}>
        {t("settings.pages.backup.intro")}
      </PageHeader>
      <div className="space-y-5">
        <BackupTab toast={toast} />
        <StorageTab toast={toast} />
      </div>
    </div>
  );
}

function AiPage({ toast }: { toast: ToastFn }) {
  return (
    <div className="space-y-8">
      <LlmTab toast={toast} />
      <EmbeddingsTab toast={toast} />
    </div>
  );
}

const DEV_SECTIONS: { id: string; label: string; hostOnly?: boolean }[] = [
  { id: "logs", label: "settings.pages.developer.sections.logs" },
  { id: "quality", label: "settings.pages.developer.sections.quality" },
  { id: "diagnostics", label: "settings.pages.developer.sections.diagnostics" },
  { id: "skills", label: "settings.pages.developer.sections.skills", hostOnly: true },
  { id: "addons", label: "settings.pages.developer.sections.addons", hostOnly: true },
];

function DeveloperPage({ toast, section, onSection, hostOnlyHidden }: {
  toast: ToastFn; section?: string; onSection: (s: string) => void; hostOnlyHidden: boolean;
}) {
  const { t } = useTranslation();
  const sections = DEV_SECTIONS.filter(d => !(d.hostOnly && hostOnlyHidden));
  const active = sections.some(d => d.id === section) ? section! : "logs";
  return (
    <div>
      <PageHeader title={t("settings.pages.developer.title")}>
        {t("settings.pages.developer.intro")}
      </PageHeader>
      <div className="flex flex-wrap gap-1.5 mb-6">
        {sections.map(d => (
          <button
            key={d.id}
            onClick={() => onSection(d.id)}
            className={cn("px-3 py-1.5 rounded-full text-sm border transition",
              active === d.id ? "bg-primary text-primary-foreground border-primary" : "border-border hover:bg-muted")}
          >
            {t(d.label)}
          </button>
        ))}
      </div>
      {active === "logs"    && <LogsTab toast={toast} />}
      {active === "quality" && <QualityTab toast={toast} />}
      {active === "diagnostics" && <DiagnosticsSection toast={toast} />}
      {active === "skills"  && <SkillsTab toast={toast} />}
      {active === "addons"  && (
        <div className="space-y-10">
          <MarketplaceTab toast={toast} />
          <InstalledAppsTab toast={toast} />
        </div>
      )}
    </div>
  );
}

// ─── Profile tab ──────────────────────────────────────────────────────

function ProfileTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  const auth = useAuth();
  const u = auth.user;
  const kid = isKid(u.role);
  const isAdmin = u.role === "admin" || u.role === "platform_admin";
  const [busy, setBusy] = useState(false);
  const initial = {
    name:             u.name || "",
    // First + last split — the letterhead signs with the full name. Fall
    // back to splitting `name` on the first space when these aren't set
    // yet (pre-migration-017 sessions).
    first_name:       u.first_name || (u.name || "").split(" ")[0] || "",
    last_name:        u.last_name  || (u.name || "").split(" ").slice(1).join(" ") || "",
    language:         u.language || "en",
    country:          u.country || "",
    address_street:   u.address_street || "",
    address_postcode: u.address_postcode || "",
    address_city:     u.address_city || "",
    phone:            u.phone || "",
  };
  const [form, setForm] = useState(initial);
  const dirty = (Object.keys(form) as (keyof typeof form)[]).some(k => form[k] !== initial[k]);

  function set<K extends keyof typeof form>(k: K, v: typeof form[K]) {
    setForm(p => ({ ...p, [k]: v }));
  }

  // Leaving the page with unsaved edits asks first (they used to vanish).
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

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
      toast(t("settings.profile.saved"), "success");
    } catch (e: any) {
      toast(t("settings.profile.saveFailed", { msg: e.message }), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">{t("settings.profile.title")}</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t("settings.profile.intro")} {kid ? "" : t("settings.profile.introLetterhead")}
        </p>
      </header>

      <div className="space-y-5">
        <Card title={t("settings.profile.you")}>
          <div className="grid grid-cols-2 gap-3">
            <Field label={t("settings.profile.firstName")}>
              <input value={form.first_name} onChange={e => set("first_name", e.target.value)} className={inputClass} />
            </Field>
            <Field label={t("settings.profile.lastName")}>
              <input value={form.last_name} onChange={e => set("last_name", e.target.value)} className={inputClass} />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-3 mt-3">
            <Field label={t("settings.profile.familyName")}>
              <input value={form.name} onChange={e => set("name", e.target.value)} className={inputClass} />
            </Field>
            {!isPlaceholderEmail(u.email) && (
              <Field label={t("settings.profile.email")}>
                <input value={u.email} disabled className={cn(inputClass, "opacity-60 cursor-not-allowed")} />
              </Field>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3 mt-3">
            <Field label={t("settings.profile.language")}>
              <select value={form.language} onChange={e => set("language", e.target.value)} className={inputClass}>
                <option value="en">English</option>
                <option value="de">Deutsch</option>
              </select>
            </Field>
            {!kid && (
              <Field label={t("settings.profile.country")}>
                <select value={form.country} onChange={e => set("country", e.target.value)} className={inputClass}>
                  <option value="">—</option>
                  <option value="DE">🇩🇪 {t("settings.profile.countries.DE")}</option>
                  <option value="AT">🇦🇹 {t("settings.profile.countries.AT")}</option>
                  <option value="CH">🇨🇭 {t("settings.profile.countries.CH")}</option>
                  <option value="US">🇺🇸 {t("settings.profile.countries.US")}</option>
                  <option value="GB">🇬🇧 {t("settings.profile.countries.GB")}</option>
                  <option value="PL">🇵🇱 {t("settings.profile.countries.PL")}</option>
                  <option value="FR">🇫🇷 {t("settings.profile.countries.FR")}</option>
                  <option value="ES">🇪🇸 {t("settings.profile.countries.ES")}</option>
                  <option value="IT">🇮🇹 {t("settings.profile.countries.IT")}</option>
                </select>
              </Field>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-2">
            {t("settings.profile.languageNote")}
            {isAdmin && ` ${t("settings.profile.adminCountryNote")}`}
          </p>
        </Card>

        {!kid && (
          <Card title={t("settings.profile.address")}>
            <Field label={t("settings.profile.street")}>
              <input value={form.address_street} onChange={e => set("address_street", e.target.value)} className={inputClass} />
            </Field>
            <div className="grid grid-cols-3 gap-3 mt-3">
              <Field label={t("settings.profile.postcode")}>
                <input value={form.address_postcode} onChange={e => set("address_postcode", e.target.value)} className={inputClass} />
              </Field>
              <div className="col-span-2">
                <Field label={t("settings.profile.city")}>
                  <input value={form.address_city} onChange={e => set("address_city", e.target.value)} className={inputClass} />
                </Field>
              </div>
            </div>
            <div className="mt-3">
              <Field label={t("settings.profile.phone")}>
                <input value={form.phone} onChange={e => set("phone", e.target.value)} className={inputClass} />
              </Field>
            </div>
          </Card>
        )}

        <div className="flex items-center justify-end gap-3">
          {dirty && <span className="text-xs text-amber-600">{t("settings.profile.notSaved")}</span>}
          <button
            onClick={save}
            disabled={busy || !dirty}
            className={cn(
              "px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-1.5 transition",
              "bg-gradient-to-r from-violet-500 to-blue-500 hover:from-violet-600 hover:to-blue-600 text-white shadow-md",
              (busy || !dirty) && "opacity-60",
              busy && "cursor-wait",
            )}
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            {t("settings.profile.saveChanges")}
          </button>
        </div>

        <ProfileLookCard toast={toast} />
        <ThemeToggle />
      </div>
    </div>
  );
}

/** Business name, tax number and bank account: what forms and invoices
 *  need. Saved on the profile; the letterhead below takes them over
 *  where it hasn't been changed by hand. */
function BusinessCard({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  const auth = useAuth();
  const u = auth.user;
  const initial = { business_name: u.business_name || "", tax_id: u.tax_id || "", iban: u.iban || "" };
  const [form, setForm] = useState(initial);
  const [busy, setBusy] = useState(false);
  const dirty = form.business_name !== initial.business_name || form.tax_id !== initial.tax_id || form.iban !== initial.iban;

  async function save() {
    setBusy(true);
    try {
      await api.patch("/api/profile", {
        business_name: form.business_name || null, tax_id: form.tax_id || null, iban: form.iban || null,
      });
      await auth.refresh();
      toast(t("settings.business.saved"), "success");
    } catch (e: any) {
      toast(t("settings.business.saveFailed", { msg: e.message }), "error");
    } finally { setBusy(false); }
  }

  return (
    <Card title={t("settings.business.title")}>
      <p className="text-xs text-muted-foreground mb-3">
        {t("settings.business.intro")}
      </p>
      <Field label={t("settings.business.businessName")}>
        <input value={form.business_name} onChange={e => setForm(f => ({ ...f, business_name: e.target.value }))} className={inputClass} />
      </Field>
      <div className="grid grid-cols-2 gap-3 mt-3">
        <Field label={t("settings.business.taxNumber")}>
          <input value={form.tax_id} onChange={e => setForm(f => ({ ...f, tax_id: e.target.value }))} className={inputClass} />
        </Field>
        <Field label={t("settings.business.iban")}>
          <input value={form.iban} onChange={e => setForm(f => ({ ...f, iban: e.target.value }))} className={cn(inputClass, "font-mono")} />
        </Field>
      </div>
      <div className="flex justify-end mt-3">
        <button
          onClick={save}
          disabled={busy || !dirty}
          className="px-3 py-1.5 rounded-md text-sm font-medium bg-primary text-primary-foreground hover:opacity-90 transition disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
          {t("settings.business.save")}
        </button>
      </div>
    </Card>
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
  const { t } = useTranslation();
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
    { id: "light",  label: t("settings.theme.light"),  icon: Sun },
    { id: "dark",   label: t("settings.theme.dark"),   icon: Moon },
    { id: "system", label: t("settings.theme.system"), icon: Monitor },
  ];

  return (
    <Card title={t("settings.theme.title")}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.theme.theme")}</div>
          <p className="text-xs text-muted-foreground mt-1">
            {t("settings.theme.intro")}
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
  const { t } = useTranslation();
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
          ? t("settings.voiceAck.on")
          : t("settings.voiceAck.off"),
        "success",
      );
    } catch (e: any) {
      toast(e.message || t("settings.voiceAck.saveFailed"), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex-1">
        <div className="text-sm font-medium">{t("settings.voiceAck.title")}</div>
        <p className="text-xs text-muted-foreground mt-0.5">
          {t("settings.voiceAck.intro")}
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

type ApiToken = {
  id: number;
  name: string;
  prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked: boolean;
};

/** Personal API tokens: how an outside agent (Hermes, Claude, a script)
 *  talks to this Yorik as the logged-in user. The token is shown once. */
function ApiTokensCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState<{ id: number; token: string } | null>(null);
  const mcpUrl = `${window.location.origin}/mcp`;
  const auth = useAuth();
  const isAdmin = ["admin", "platform_admin"].includes(String(auth.user?.role || "").toLowerCase());
  const [household, setHousehold] = useState<(ApiToken & { owner: string; owner_id: string })[]>([]);

  const load = useCallback(async () => {
    try {
      setTokens(await api.get<ApiToken[]>("/api/tokens"));
      if (isAdmin) {
        setHousehold(await api.get<(ApiToken & { owner: string; owner_id: string })[]>("/api/tokens/all"));
      }
    } catch (e: any) {
      toast(t("settings.apiTokens.loadFailed", { msg: e.message }), "error");
    }
  }, [toast, isAdmin, t]);
  useEffect(() => { load(); }, [load]);

  async function create() {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api.post<ApiToken & { token: string }>("/api/tokens", { name: name.trim() });
      setFresh({ id: r.id, token: r.token });
      setName("");
      await load();
    } catch (e: any) {
      toast(t("settings.apiTokens.createFailed", { msg: e.message }), "error");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: number) {
    if (busy) return;
    setBusy(true);
    try {
      await api.delete(`/api/tokens/${id}`);
      if (fresh?.id === id) setFresh(null);
      await load();
      toast(t("settings.apiTokens.revoked"), "success");
    } catch (e: any) {
      toast(t("settings.apiTokens.revokeFailed", { msg: e.message }), "error");
    } finally {
      setBusy(false);
    }
  }

  async function copy(text: string, what: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast(t("settings.apiTokens.copied", { what }), "success");
    } catch {
      toast(t("settings.apiTokens.copyFailed"), "error");
    }
  }

  const live = tokens.filter(t => !t.revoked);

  return (
    <Card title={t("settings.apiTokens.title")}>
      <div className="mb-3 flex items-start gap-2">
        <KeyRound className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.apiTokens.heading")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("settings.apiTokens.intro")}
          </p>
          <button
            type="button"
            onClick={() => copy(mcpUrl, t("settings.apiTokens.endpoint"))}
            className="mt-1 inline-flex items-center gap-1 text-xs font-mono text-foreground/80 hover:text-foreground transition"
            title={t("settings.apiTokens.copyEndpoint")}
          >
            {mcpUrl}
            <Copy className="w-3 h-3" />
          </button>
        </div>
      </div>

      <div className="flex gap-2">
        <input
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder={t("settings.apiTokens.namePlaceholder")}
          className={inputClass}
          maxLength={80}
          onKeyDown={e => { if (e.key === "Enter") create(); }}
        />
        <button
          type="button"
          onClick={create}
          disabled={busy}
          className="h-9 px-3 rounded-md bg-primary text-primary-foreground text-sm inline-flex items-center gap-1.5 disabled:opacity-50 shrink-0"
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
          {t("settings.apiTokens.create")}
        </button>
      </div>

      {fresh && (
        <div className="mt-3 rounded-md border border-amber-300/60 bg-amber-50 dark:bg-amber-950/30 dark:border-amber-700/60 p-3">
          <div className="text-xs text-amber-800 dark:text-amber-300 mb-1.5">
            {t("settings.apiTokens.showOnce")}
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 text-xs font-mono break-all select-all">{fresh.token}</code>
            <button
              type="button"
              onClick={() => copy(fresh.token, t("settings.apiTokens.token"))}
              className="text-xs inline-flex items-center gap-1 text-muted-foreground hover:text-foreground transition shrink-0"
            >
              <Copy className="w-3.5 h-3.5" /> {t("settings.apiTokens.copy")}
            </button>
          </div>
        </div>
      )}

      {live.length > 0 && (
        <ul className="mt-3 divide-y divide-border">
          {live.map(tok => (
            <li key={tok.id} className="py-2 flex items-center gap-3">
              <div className="flex-1 min-w-0">
                <div className="text-sm truncate">{tok.name}</div>
                <div className="text-xs text-muted-foreground font-mono">
                  {tok.prefix}
                  <span className="font-sans"> · {t("settings.apiTokens.created", { date: tok.created_at?.slice(0, 10) ?? "?" })}
                    {tok.last_used_at ? ` · ${t("settings.apiTokens.lastUsed", { date: tok.last_used_at.slice(0, 16) })}` : ` · ${t("settings.apiTokens.neverUsed")}`}
                  </span>
                </div>
              </div>
              <button
                type="button"
                onClick={() => revoke(tok.id)}
                disabled={busy}
                className="text-xs inline-flex items-center gap-1 text-muted-foreground hover:text-red-600 transition disabled:opacity-50"
                title={t("settings.apiTokens.revoke")}
              >
                <Trash2 className="w-3.5 h-3.5" /> {t("settings.apiTokens.revoke")}
              </button>
            </li>
          ))}
        </ul>
      )}
      {live.length === 0 && !fresh && (
        <p className="mt-3 text-xs text-muted-foreground">{t("settings.apiTokens.none")}</p>
      )}
      {isAdmin && household.some(t => t.owner_id !== String(auth.user?.id)) && (
        <div className="mt-4 pt-3 border-t border-border">
          <div className="text-xs text-muted-foreground mb-1">
            {t("settings.apiTokens.others")}
          </div>
          <ul className="divide-y divide-border">
            {household.filter(tok => tok.owner_id !== String(auth.user?.id)).map(tok => (
              <li key={tok.id} className="py-2 flex items-center gap-3">
                <div className="flex-1 min-w-0">
                  <div className="text-sm truncate">{tok.owner}: {tok.name}</div>
                  <div className="text-xs text-muted-foreground font-mono">
                    {tok.prefix}
                    <span className="font-sans">
                      {tok.last_used_at ? ` · ${t("settings.apiTokens.lastUsed", { date: tok.last_used_at.slice(0, 16) })}` : ` · ${t("settings.apiTokens.neverUsed")}`}
                    </span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => revoke(tok.id)}
                  disabled={busy}
                  className="text-xs inline-flex items-center gap-1 text-muted-foreground hover:text-red-600 transition disabled:opacity-50"
                >
                  <Trash2 className="w-3.5 h-3.5" /> {t("settings.apiTokens.revoke")}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

function ChangePasswordCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
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
      toast(t("settings.password.changed"), "success");
    } catch (e: any) {
      // 401 = current password wrong; 400 = new too short / other validation
      const msg = e?.message || t("settings.password.changeFailed");
      toast(msg.includes("401") || msg.toLowerCase().includes("current")
        ? t("settings.password.currentWrong")
        : msg, "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title={t("settings.password.title")}>
      <div className="mb-3 flex items-start gap-2">
        <KeyRound className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.password.heading")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("settings.password.intro")}
          </p>
        </div>
        <button
          onClick={() => setShow(s => !s)}
          className="text-xs text-muted-foreground hover:text-foreground transition shrink-0"
          type="button"
        >
          {show ? t("settings.password.hide") : t("settings.password.show")}
        </button>
      </div>
      <div className="space-y-2.5">
        <Field label={t("settings.password.current")}>
          <input
            type={show ? "text" : "password"}
            value={currentPw}
            onChange={e => setCurrentPw(e.target.value)}
            autoComplete="current-password"
            className={inputClass}
          />
        </Field>
        <Field label={t("settings.password.new")}>
          <input
            type={show ? "text" : "password"}
            value={newPw}
            onChange={e => setNewPw(e.target.value)}
            autoComplete="new-password"
            className={inputClass}
          />
        </Field>
        {newTooShort && (
          <p className="text-xs text-amber-600 dark:text-amber-500 -mt-1.5">
            {t("settings.password.tooShort")}
          </p>
        )}
        <Field label={t("settings.password.confirm")}>
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
          <p className="text-xs text-amber-600 dark:text-amber-500 -mt-1.5">
            {t("settings.password.mismatch")}
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
          {t("settings.password.submit")}
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
  const { t } = useTranslation();
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
      toast(t("settings.kioskPin.saved"), "success");
    } catch (e: any) {
      toast(e?.message || t("settings.kioskPin.setFailed"), "error");
    } finally {
      setSaving(false);
    }
  }

  async function clear() {
    if (!confirm(t("settings.kioskPin.removeConfirm"))) return;
    setSaving(true);
    try {
      await api.delete("/api/profile/pin");
      setMeHasPin(false);
      setPinSetAt(null);
      toast(t("settings.kioskPin.removed"), "success");
    } catch (e: any) {
      toast(e?.message || t("settings.kioskPin.clearFailed"), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title={t("settings.kioskPin.title")}>
      <div className="mb-3 flex items-start gap-2">
        <KeyRound className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.kioskPin.heading")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("settings.kioskPin.intro")}
          </p>
        </div>
      </div>

      {meHasPin === true && (
        <div className="mb-3 rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-700 dark:text-emerald-400 flex items-center justify-between gap-2">
          <span>
            {pinSetAt ? t("settings.kioskPin.setOn", { date: new Date(pinSetAt).toLocaleDateString(locale()) }) : t("settings.kioskPin.set")}
          </span>
          <button
            onClick={clear}
            disabled={saving}
            className="text-xs underline hover:no-underline disabled:opacity-50"
          >
            {t("settings.kioskPin.clear")}
          </button>
        </div>
      )}

      <div className="space-y-2.5">
        <Field label={meHasPin ? t("settings.kioskPin.newPin") : t("settings.kioskPin.pin")}>
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
        <Field label={t("settings.kioskPin.confirm")}>
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
          <p className="text-xs text-amber-600 dark:text-amber-500 -mt-1.5">
            {t("settings.kioskPin.exactlyFour")}
          </p>
        )}
        {pin2.length === 4 && !matches && (
          <p className="text-xs text-amber-600 dark:text-amber-500 -mt-1.5">
            {t("settings.kioskPin.mismatch")}
          </p>
        )}
      </div>

      <div className="flex items-center justify-end gap-2 mt-4">
        <button
          onClick={() => setShow(s => !s)}
          className="text-xs text-muted-foreground hover:text-foreground transition"
          type="button"
        >
          {show ? t("settings.kioskPin.hideDigits") : t("settings.kioskPin.showDigits")}
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
          {meHasPin ? t("settings.kioskPin.update") : t("settings.kioskPin.setPin")}
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
  const { t } = useTranslation();
  const auth = useAuth();
  const myId = auth.user.id;

  // Recording target — long enough to give ECAPA a stable embedding,
  // short enough that nobody dreads the prompt. Backend MIN is 2s.
  const TARGET_SECONDS = 10;
  // Sample prompt — phonetically varied so the embedding covers
  // common vowels + consonants. Households can re-record any time
  // if they want to try a different prompt.
  const PROMPT = t("settings.voiceEnrollment.prompt");

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
      setError(t("settings.voiceEnrollment.noSupport"));
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
        ? t("settings.voiceEnrollment.micDenied")
        : (err?.message || t("settings.voiceEnrollment.micFailed"));
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
        let msg = text || `HTTP ${r.status}`;
        try { const j = JSON.parse(text); msg = j.detail || j.message || msg; } catch { /* plain text */ }
        throw new Error(msg);
      }
      toast(t("settings.voiceEnrollment.enrolled"), "success");
      await refresh();
    } catch (err: any) {
      const msg = err?.message || t("settings.voiceEnrollment.failed");
      setError(msg);
      toast(t("settings.voiceEnrollment.failedWith", { msg }), "error");
    } finally {
      setUploading(false);
    }
  }

  async function clearEnrollment() {
    if (!confirm(t("settings.voiceEnrollment.removeConfirm"))) return;
    setUploading(true);
    try {
      const r = await fetch(`/api/voice-profile/${myId}/enrollment`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
      toast(t("settings.voiceEnrollment.cleared"), "success");
      await refresh();
    } catch (err: any) {
      toast(err?.message || t("settings.voiceEnrollment.clearFailed"), "error");
    } finally {
      setUploading(false);
    }
  }

  return (
    <Card title={t("settings.voiceEnrollment.title")}>
      <div className="mb-3 flex items-start gap-2">
        <Mic className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.voiceEnrollment.heading")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("settings.voiceEnrollment.intro")}
          </p>
        </div>
      </div>

      {enrolled === true && !recording && !uploading && (
        <div className="mb-3 rounded-md border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-700 dark:text-emerald-400 flex items-center justify-between gap-2">
          <span>{t("settings.voiceEnrollment.enrolledNote")}</span>
          <button
            onClick={clearEnrollment}
            disabled={uploading}
            className="text-xs underline hover:no-underline disabled:opacity-50"
          >
            {t("settings.voiceEnrollment.clear")}
          </button>
        </div>
      )}

      {error && (
        <div className="rounded-md border border-rose-500/30 bg-rose-500/5 px-3 py-2 text-xs text-rose-700 dark:text-rose-400 mb-3">
          {error}
        </div>
      )}

      <div className="rounded-md border border-violet-500/20 bg-violet-500/5 px-3 py-2 text-xs text-foreground/80 mb-3 italic">
        {t("settings.voiceEnrollment.readAloud")} <span className="not-italic">"{PROMPT}"</span>
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
            {enrolled ? t("settings.voiceEnrollment.reEnroll") : t("settings.voiceEnrollment.record")}
          </button>
        )}

        {recording && (
          <button
            onClick={stopRecording}
            className="px-4 py-2 rounded-md font-medium text-sm inline-flex items-center gap-1.5 bg-rose-500 text-white shadow-md hover:bg-rose-600"
          >
            <Square className="w-4 h-4" />
            {t("settings.voiceEnrollment.stop", { s: remaining })}
          </button>
        )}

        {uploading && (
          <span className="text-xs text-muted-foreground inline-flex items-center gap-1.5">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            {t("settings.voiceEnrollment.enrolling")}
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

interface ParakeetVariant {
  id: "de" | "multi";
  label: string;
  size_mb: number;
  blurb: string;
  installed: boolean;
}

interface ParakeetStatus {
  model: ParakeetVariant["id"];
  variants: ParakeetVariant[];
  download: { state: "idle" | "running" | "done" | "error"; variant: string; message: string };
}

interface STTBackend {
  id: "parakeet" | "whisper" | "groq" | "openai-compatible";
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
  whisper_available?: boolean;
  parakeet: ParakeetStatus;
}

function STTConfigCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
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

  // Parakeet (CPU) variant + on-demand model download.
  const [parakeet, setParakeet] = useState<ParakeetStatus | null>(null);
  const [whisperAvailable, setWhisperAvailable] = useState(true);
  const [busyVariant, setBusyVariant] = useState<string | null>(null);

  const loadConfig = useCallback(async () => {
    const r = await api.get<STTConfigResponse>("/api/voice/config");
    setCurrentModel(r.stt_model);
    setCatalogue(r.catalogue || []);
    setBackend(r.stt_backend);
    setBackends(r.backends || []);
    setUrl(r.stt_url || "");
    setModelName(r.stt_model_name || "");
    setApiKeySet(!!r.stt_api_key_set);
    setParakeet(r.parakeet || null);
    setWhisperAvailable(r.whisper_available !== false);
    return r;
  }, []);

  useEffect(() => {
    loadConfig().catch((e: any) => toast(t("settings.stt.loadFailed", { msg: e.message }), "error"));
  }, [loadConfig, toast, t]);

  // A model picked before it was downloaded: switch once it's there.
  const switchAfterRef = useRef<ParakeetVariant["id"] | null>(null);

  // While a download runs, poll every 3 s until it settles.
  const downloading = parakeet?.download.state === "running";
  useEffect(() => {
    if (!downloading) return;
    const timer = setInterval(() => {
      loadConfig().then(r => {
        if (r.parakeet?.download.state === "done") {
          const next = switchAfterRef.current;
          switchAfterRef.current = null;
          if (next) void pickVariant(next);
          else toast(t("settings.stt.modelDownloaded"), "success");
        }
        if (r.parakeet?.download.state === "error") {
          switchAfterRef.current = null;
          toast(t("settings.stt.downloadFailed", { msg: r.parakeet.download.message }), "error");
        }
      }).catch(() => {});
    }, 3000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downloading, loadConfig, toast, t]);

  async function pickVariant(id: ParakeetVariant["id"]) {
    if (!parakeet || id === parakeet.model || busyVariant) return;
    const meta = parakeet.variants.find(v => v.id === id);
    if (meta && !meta.installed) {
      // Download first; the current model keeps working meanwhile.
      switchAfterRef.current = id;
      await downloadVariant(id);
      return;
    }
    setBusyVariant(id);
    try {
      const r = await api.patch<STTConfigResponse>("/api/voice/config", { parakeet_model: id });
      setParakeet(r.parakeet);
      toast(t("settings.stt.switchedTo", { name: meta?.label || id }), "success");
    } catch (e: any) {
      toast(e.message || t("settings.stt.switchFailed"), "error");
    } finally {
      setBusyVariant(null);
    }
  }

  async function downloadVariant(id: ParakeetVariant["id"]) {
    try {
      const r = await api.post<{ ok: boolean; installed: boolean; download: ParakeetStatus["download"] }>(
        "/api/voice/parakeet/download", { model: id },
      );
      setParakeet(p => p ? { ...p, download: r.download } : p);
      if (!r.installed) toast(switchAfterRef.current === id
        ? t("settings.stt.downloadingThenSwitch")
        : t("settings.stt.downloadingBackground"), "info");
    } catch (e: any) {
      toast(e.message || t("settings.stt.downloadStartFailed"), "error");
    }
  }

  async function pickModel(id: string) {
    if (id === currentModel) return;
    setBusyModel(id);
    try {
      await api.patch("/api/voice/config", { stt_model: id });
      setCurrentModel(id);
      const meta = catalogue.find(m => m.id === id);
      toast(
        `${t("settings.stt.switchedTo", { name: meta?.label || id })} ${meta && meta.size_mb > 200
          ? t("settings.stt.willDownload", { mb: meta.size_mb })
          : t("settings.stt.activeNext")}`,
        "success",
      );
    } catch (e: any) {
      toast(e.message || t("settings.stt.switchFailed"), "error");
    } finally {
      setBusyModel(null);
    }
  }

  // Switch engine. For cloud engines we PRE-FILL URL + model name
  // from the catalogue so the user only has to paste a key.
  function selectBackend(id: STTBackend["id"]) {
    if (id === backend) return;
    const prev = backends.find(b => b.id === backend);
    const wasLocal = backend === "whisper" || backend === "parakeet";
    setBackend(id);
    const meta = backends.find(b => b.id === id);
    if (meta) {
      // Take the new engine's address and model unless the user typed
      // their own (Groq → OpenAI-compatible kept Groq's address).
      if (!url || wasLocal || url === prev?.default_url) setUrl(meta.default_url);
      if (!modelName || wasLocal || modelName === prev?.default_model) setModelName(meta.default_model);
      // A key belongs to one provider: switching providers asks for a
      // new one instead of quietly sending the old one elsewhere.
      if (!wasLocal && meta.requires_key && prev && prev.id !== id) setApiKeySet(false);
    }
    // Don't auto-save yet — wait for the user to paste a key and
    // click Save. This avoids switching the active backend before
    // it's actually usable.
    if (id === "whisper" || id === "parakeet") {
      // Local engines have no key/url to fill; flip the active
      // backend immediately so the previous cloud config doesn't
      // keep serving requests.
      saveBackend({ stt_backend: id },
        id === "parakeet" ? t("settings.stt.switchedParakeet") : t("settings.stt.switchedWhisper"));
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
      toast(e.message || t("settings.stt.saveFailed"), "error");
    } finally {
      setSavingBackend(false);
    }
  }

  async function saveCloudConfig() {
    const meta = backends.find(b => b.id === backend);
    const trimmedKey = apiKeyInput.trim();
    if (meta?.requires_key && !trimmedKey && !apiKeySet) {
      toast(t("settings.stt.pasteKeyFirst"), "error");
      return;
    }
    const payload: Record<string, unknown> = {
      stt_backend:    backend,
      stt_url:        url.trim(),
      stt_model_name: modelName.trim(),
    };
    if (trimmedKey) payload.stt_api_key = trimmedKey;
    await saveBackend(payload, t("settings.stt.savedActive", { name: meta?.label || backend }));
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
        toast(r.note || t("settings.stt.connectionOk"), "success");
      } else {
        toast(t("settings.stt.connectionFailed", { msg: r.error || t("settings.stt.unknownError") }), "error");
      }
    } catch (e: any) {
      toast(e.message || t("settings.stt.testFailed"), "error");
    } finally {
      setTesting(false);
    }
  }

  const currentBackendMeta = backends.find(b => b.id === backend);
  const isCloud = backend !== "whisper" && backend !== "parakeet";

  return (
    <Card title={t("settings.stt.title")}>
      <div className="mb-3 flex items-start gap-2">
        <Mic className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.stt.heading")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("settings.stt.introBefore")}<strong>Parakeet</strong>{t("settings.stt.introAfter")}
          </p>
        </div>
      </div>

      {/* Engine picker */}
      <div className="space-y-1.5 mb-4">
        {backends.map(b => {
          const active = backend === b.id;
          const Icon = b.id === "parakeet" ? Cpu : b.id === "whisper" ? HardDrive : Globe;
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
                  <span className="text-2xs text-muted-foreground font-mono">
                    {t("settings.stt.apiKey")}
                  </span>
                )}
              </div>
              <div className="text-xs text-muted-foreground mt-1 ml-6">
                {b.blurb}
              </div>
            </button>
          );
        })}
      </div>

      {/* Parakeet variant + model download */}
      {backend === "parakeet" && parakeet && (
        <div className="space-y-1.5 mb-4 border border-border rounded-md p-3 bg-muted/30">
          <div className="text-xs font-medium mb-1">{t("settings.stt.parakeetModel")}</div>
          {parakeet.variants.map(v => {
            const active = parakeet.model === v.id;
            const isBusy = busyVariant === v.id;
            const dl = parakeet.download;
            const running = dl.state === "running" && dl.variant === v.id;
            return (
              <div
                key={v.id}
                className={cn(
                  "border rounded-md px-3 py-2 transition flex items-start gap-2",
                  active ? "border-violet-500 bg-violet-500/5" : "border-border bg-card",
                )}
              >
                <button
                  type="button"
                  onClick={() => pickVariant(v.id)}
                  disabled={busyVariant !== null}
                  className="flex-1 min-w-0 text-left"
                >
                  <div className="flex items-center gap-2">
                    {active
                      ? <CheckCircle2 className="w-4 h-4 text-violet-500 shrink-0" />
                      : isBusy
                      ? <Loader2 className="w-4 h-4 animate-spin shrink-0" />
                      : <div className="w-4 h-4 rounded-full border border-border shrink-0" />}
                    <span className="font-medium text-sm">{v.label}</span>
                    <span className="text-2xs text-muted-foreground font-mono">~{v.size_mb} MB</span>
                    {v.installed
                      ? <span className="text-2xs text-emerald-600">{t("settings.stt.downloaded")}</span>
                      : <span className="text-2xs text-amber-600">{t("settings.stt.notDownloaded")}</span>}
                  </div>
                  <div className="text-xs text-muted-foreground mt-1 ml-6">{v.blurb}</div>
                </button>
                {!v.installed && (
                  <button
                    type="button"
                    onClick={() => downloadVariant(v.id)}
                    disabled={dl.state === "running"}
                    className="text-xs px-2.5 py-1.5 rounded border border-border bg-card hover:bg-muted disabled:opacity-60 inline-flex items-center gap-1.5 shrink-0"
                  >
                    {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
                    {running ? t("settings.stt.downloading") : t("settings.stt.download")}
                  </button>
                )}
              </div>
            );
          })}
          {parakeet.download.state === "error" && (
            <p className="text-xs text-red-600">{parakeet.download.message}</p>
          )}
        </div>
      )}

      {/* Cloud-engine settings (URL + key + model + test) */}
      {isCloud && currentBackendMeta && (
        <div className="space-y-2.5 mb-4 border border-border rounded-md p-3 bg-muted/30">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-3.5 h-3.5 text-amber-500 mt-0.5 shrink-0" />
            <p className="text-xs text-muted-foreground">
              {t("settings.stt.cloudNoteBefore")}<span className="font-mono">{currentBackendMeta.label}</span>{t("settings.stt.cloudNoteAfter")}
            </p>
          </div>

          <div>
            <label className="block text-xs text-muted-foreground mb-1">{t("settings.stt.endpointUrl")}</label>
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
            <label className="block text-xs text-muted-foreground mb-1">{t("settings.stt.model")}</label>
            <input
              type="text"
              value={modelName}
              onChange={e => setModelName(e.target.value)}
              placeholder={currentBackendMeta.default_model}
              className="w-full text-xs font-mono px-2 py-1.5 rounded border border-border bg-background"
            />
          </div>

          <div>
            <label className="block text-xs text-muted-foreground mb-1">
              {t("settings.stt.apiKey")} {apiKeySet && !apiKeyInput && (
                <span className="text-emerald-600 font-mono">{t("settings.stt.keySaved")}</span>
              )}
            </label>
            <input
              type="password"
              value={apiKeyInput}
              onChange={e => setApiKeyInput(e.target.value)}
              placeholder={apiKeySet ? t("settings.stt.keyOnFile") : t("settings.stt.pasteKey")}
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
              {t("settings.stt.save")}
            </button>
            <button
              onClick={testConnection}
              disabled={testing || (!apiKeySet && !apiKeyInput)}
              className="text-xs px-3 py-1.5 rounded border border-border bg-card hover:bg-muted disabled:opacity-60 inline-flex items-center gap-1.5"
            >
              {testing
                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                : <Plug className="w-3.5 h-3.5" />}
              {t("settings.stt.testConnection")}
            </button>
          </div>
        </div>
      )}

      {/* Whisper model picker — only when the optional whisper package
          is installed. */}
      {whisperAvailable && <div className="border-t border-border pt-3 mt-1">
        <div className="flex items-start gap-2 mb-2">
          <HardDrive className="w-3.5 h-3.5 text-muted-foreground mt-0.5 shrink-0" />
          <div className="flex-1">
            <div className="text-xs font-medium">{t("settings.stt.whisperModel")}</div>
            <p className="text-xs text-muted-foreground mt-0.5">
              {backend !== "whisper"
                ? t("settings.stt.whisperFallbackHint")
                : t("settings.stt.whisperActiveHint")}
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
                    <span className="text-2xs text-muted-foreground font-mono">{m.id}</span>
                  </div>
                  <div className="text-2xs text-muted-foreground text-right tabular-nums shrink-0">
                    ~{m.size_mb} MB · {m.ms_short < 1000 ? `${m.ms_short}ms` : `${(m.ms_short/1000).toFixed(1)}s`}
                  </div>
                </div>
                <div className="text-xs text-muted-foreground mt-1 ml-6">{m.blurb}</div>
              </button>
            );
          })}
        </div>
      </div>}
    </Card>
  );
}


// ─── Confirm mutations toggle ─────────────────────────────────────────
// Beta safety net: when ON, LLM-initiated create/update/delete actions
// show a confirmation modal before they happen. Decision feeds the
// per-model quality dashboard (Settings → Quality).

// ─── Emergency access — "Zugriff auf alles" for the adults, logged and announced ───

interface EmergencyRow {
  id: number; user_id: string; user_name: string | null; reason: string;
  started_at: string; expires_at: string; ended_at: string | null; active: boolean;
}

function EmergencyAccessCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
  const auth = useAuth();
  const isAdult = String(auth.user?.role || "").toLowerCase() !== "restricted";
  const [mine, setMine] = useState<EmergencyRow | null>(null);
  const [history, setHistory] = useState<EmergencyRow[]>([]);
  const [maxHours, setMaxHours] = useState(24);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [hours, setHours] = useState(24);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const r = await api.get<{ mine: EmergencyRow | null; history: EmergencyRow[]; max_hours: number }>("/api/emergency-access");
      setMine(r.mine); setHistory(r.history); setMaxHours(r.max_hours);
    } catch { /* not an adult, or an old backend */ }
  };
  useEffect(() => { if (isAdult) void load(); }, [isAdult]);

  if (!isAdult) return null;
  const fmt = (iso: string) => new Date(iso).toLocaleString(locale(), { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

  async function start() {
    setBusy(true);
    try {
      await api.post("/api/emergency-access", { reason, hours, password });
      setOpen(false); setReason(""); setPassword("");
      toast(t("settings.emergency.started"), "success");
      await load();
    } catch (e: any) {
      toast(e.message || t("settings.emergency.startFailed"), "error");
    } finally { setBusy(false); }
  }
  async function end() {
    if (!mine) return;
    setBusy(true);
    try { await api.delete(`/api/emergency-access/${mine.id}`); toast(t("settings.emergency.ended"), "success"); await load(); }
    catch (e: any) { toast(e.message || t("settings.emergency.endFailed"), "error"); }
    finally { setBusy(false); }
  }

  return (
    <Card title={t("settings.emergency.title")}>
      <p className="text-xs text-muted-foreground">
        {t("settings.emergency.explain", { maxHours })}
      </p>
      {mine ? (
        <div className="mt-3 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm">
          <div className="font-medium text-red-600 dark:text-red-400">{t("settings.emergency.activeUntil", { until: fmt(mine.expires_at) })}</div>
          <div className="text-xs text-muted-foreground mt-1">{t("settings.emergency.reason", { reason: mine.reason })}</div>
          <button onClick={end} disabled={busy}
                  className="mt-2 px-3 h-9 rounded-md border border-border hover:bg-muted text-sm disabled:opacity-50">
            {t("settings.emergency.endNow")}
          </button>
        </div>
      ) : open ? (
        <div className="mt-3 space-y-2">
          <textarea value={reason} onChange={e => setReason(e.target.value)} rows={2}
                    placeholder={t("settings.emergency.reasonPlaceholder")}
                    className="w-full rounded-md border border-border bg-background p-2 text-sm" />
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-xs text-muted-foreground">{t("settings.emergency.duration")}</label>
            <input type="number" min={1} max={maxHours} value={hours} onChange={e => setHours(Number(e.target.value))}
                   className="w-20 rounded-md border border-border bg-background p-1.5 text-sm" />
            <span className="text-xs text-muted-foreground">{t("settings.emergency.hours", { count: hours })}</span>
            <input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder={t("settings.emergency.yourPassword")}
                   autoComplete="current-password"
                   className="flex-1 min-w-[10rem] rounded-md border border-border bg-background p-1.5 text-sm" />
          </div>
          <div className="flex gap-2">
            <button onClick={start} disabled={busy || reason.trim().length < 10 || !password}
                    className="px-3 h-9 rounded-md bg-red-600 text-white hover:opacity-90 text-sm disabled:opacity-50">
              {t("settings.emergency.turnOn")}
            </button>
            <button onClick={() => setOpen(false)} disabled={busy}
                    className="px-3 h-9 rounded-md border border-border hover:bg-muted text-sm">{t("common.cancel")}</button>
          </div>
        </div>
      ) : (
        <button onClick={() => setOpen(true)}
                className="mt-3 px-3 h-9 rounded-md border border-red-500/50 text-red-600 dark:text-red-400 hover:bg-red-500/10 text-sm">
          {t("settings.emergency.turnOnEllipsis")}
        </button>
      )}
      {history.length > 0 && (
        <div className="mt-4">
          <div className="text-xs font-medium mb-1">{t("settings.emergency.history")}</div>
          <ul className="space-y-1 text-xs text-muted-foreground">
            {history.slice(0, 10).map(h => (
              <li key={h.id}>
                <span className={cn(h.active && "text-red-600 dark:text-red-400 font-medium")}>{h.user_name || h.user_id}</span>
                {" · "}{t("settings.emergency.range", { from: fmt(h.started_at), to: fmt(h.ended_at || h.expires_at) })}{h.active ? ` ${t("settings.emergency.running")}` : ""}
                {" · "}{h.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

// ─── Kiosk photos consent — show MY photos of the day on the household wall ───

function KioskPhotosConsentCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.get<{ consent: boolean }>("/api/users/me/kiosk-photos-consent");
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
      await api.patch("/api/users/me/kiosk-photos-consent", { consent: next });
      setEnabled(next);
      toast(next
        ? t("settings.kioskPhotos.on")
        : t("settings.kioskPhotos.off"),
        "success");
    } catch (e: any) {
      toast(e.message || t("settings.kioskPhotos.saveFailed"), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title={t("settings.kioskPhotos.title")}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.kioskPhotos.heading")}</div>
          <p className="text-xs text-muted-foreground mt-1">
            {t("settings.kioskPhotos.explain")}
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

// ─── Kiosk agenda consent — show MY appointments on the household wall ───

function KioskAgendaConsentCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
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
        ? t("settings.kioskAgenda.on")
        : t("settings.kioskAgenda.off"),
        "success");
    } catch (e: any) {
      toast(e.message || t("settings.kioskAgenda.saveFailed"), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title={t("settings.kioskAgenda.title")}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.kioskAgenda.heading")}</div>
          <p className="text-xs text-muted-foreground mt-1">
            {t("settings.kioskAgenda.explain")}
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


function ConfirmMutationsToggle({ toast, part }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
  /** "undo": the Undo card after Yorik's changes (everyone).
   *  "agents": whether an outside agent may confirm deletions. */
  part: "undo" | "agents";
}) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [agentDeletes, setAgentDeletes] = useState<boolean | null>(null);
  const [savingAgent, setSavingAgent] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.get<{ logged_in: boolean; user?: any }>("/api/auth/me");
        setEnabled(!!r.user?.confirm_mutations);
        setAgentDeletes(!!r.user?.agent_may_confirm_deletes);
      } catch {
        setEnabled(true);  // safe default
        setAgentDeletes(false);
      }
    })();
  }, []);

  async function toggleAgentDeletes() {
    if (agentDeletes === null) return;
    const next = !agentDeletes;
    setSavingAgent(true);
    try {
      await api.patch("/api/profile/agent-deletes", { enabled: next });
      setAgentDeletes(next);
      toast(next
        ? t("settings.confirmMutations.agentsOn")
        : t("settings.confirmMutations.agentsOff"),
        "success");
    } catch (e: any) {
      toast(e.message || t("settings.confirmMutations.saveFailed"), "error");
    } finally {
      setSavingAgent(false);
    }
  }

  async function toggle() {
    if (enabled === null) return;
    const next = !enabled;
    setSaving(true);
    try {
      await api.patch("/api/profile/confirm-mutations", { enabled: next });
      setEnabled(next);
      toast(next
        ? t("settings.confirmMutations.undoOn")
        : t("settings.confirmMutations.undoOff"),
        "success");
    } catch (e: any) {
      toast(e.message || t("settings.confirmMutations.saveFailed"), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    part === "undo" ? (
    <Card title={t("settings.confirmMutations.undoTitle")}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.confirmMutations.undoHeading")}</div>
          <p className="text-xs text-muted-foreground mt-1">
            {t("settings.confirmMutations.undoExplain")}
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
          aria-label={t("settings.confirmMutations.undoAria")}
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
    ) : (
    <Card title={t("settings.confirmMutations.agentsTitle")}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.confirmMutations.agentsHeading")}</div>
          <p className="text-xs text-muted-foreground mt-1">
            {t("settings.confirmMutations.agentsExplain")}
          </p>
        </div>
        <button
          onClick={toggleAgentDeletes}
          disabled={agentDeletes === null || savingAgent}
          className={cn(
            "shrink-0 relative inline-flex h-6 w-11 items-center rounded-full transition",
            agentDeletes ? "bg-violet-500" : "bg-muted",
            (agentDeletes === null || savingAgent) && "opacity-60 cursor-wait",
          )}
          aria-pressed={!!agentDeletes}
          aria-label={t("settings.confirmMutations.agentsHeading")}
        >
          <span
            className={cn(
              "inline-block h-4 w-4 transform rounded-full bg-white transition",
              agentDeletes ? "translate-x-6" : "translate-x-1",
            )}
          />
        </button>
      </div>
    </Card>
    )
  );
}

// ─── Suggestion engine ─ master toggle + per-source switches ───────

function SuggestionEngineCard({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
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
        ? t("settings.suggestions.on")
        : t("settings.suggestions.off"),
        "success");
    } catch (e: any) {
      toast(e.message || t("settings.suggestions.saveFailed"), "error");
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
      toast(e.message || t("settings.suggestions.saveFailed"), "error");
    } finally {
      setSaving("");
    }
  }

  const KNOWN_SOURCES: Array<{ key: string; label: string; hint?: string }> = [
    { key: "email", label: t("settings.suggestions.sources.email"), hint: t("settings.suggestions.sources.emailHint") },
    { key: "wa",    label: t("settings.suggestions.sources.wa"),    hint: t("settings.suggestions.sources.waHint") },
  ];

  return (
    <Card title={t("settings.suggestions.title")}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.suggestions.heading")}</div>
          <p className="text-xs text-muted-foreground mt-1">
            {t("settings.suggestions.explain")}
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
          <div className="text-xs text-muted-foreground font-medium">
            {t("settings.suggestions.sourcesHeading")}
          </div>
          {KNOWN_SOURCES.map((src) => (
            <div key={src.key} className="flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-medium capitalize">{src.label}</div>
                {src.hint && <div className="text-xs text-muted-foreground">{src.hint}</div>}
              </div>
              <button
                onClick={() => toggleSource(src.key)}
                disabled={saving === src.key}
                className={cn(
                  "shrink-0 relative inline-flex h-5 w-9 items-center rounded-full transition",
                  sources[src.key] ? "bg-violet-500" : "bg-muted",
                  saving === src.key && "opacity-50 cursor-wait",
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
  const { t } = useTranslation();
  const auth = useAuth();
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
      await auth.refresh();   // the chat reads it from the signed-in user
      setEnabled(next);
      toast(next
        ? t("settings.devMode.on")
        : t("settings.devMode.off"),
        "success");
    } catch (e: any) {
      toast(e.message || t("settings.devMode.saveFailed"), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title={t("settings.devMode.title")}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="text-sm font-medium">{t("settings.devMode.heading")}</div>
          <p className="text-xs text-muted-foreground mt-1">
            {t("settings.devMode.explainBefore")}<span className="font-mono opacity-80">▼ Debug</span>{t("settings.devMode.explainAfter")}
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

type DocVisibility = "private" | "parents" | "business" | "shared";
const DEFAULT_DOC_VIS_OPTIONS: { value: DocVisibility; label: string; emoji: string; desc: string }[] = [
  // label/desc are i18n keys, resolved with t() at render time.
  { value: "private",  emoji: "🔒", label: "settings.docVisibility.options.private",  desc: "settings.docVisibility.options.privateDesc" },
  { value: "parents",  emoji: "👪", label: "settings.docVisibility.options.parents",  desc: "settings.docVisibility.options.parentsDesc" },
  { value: "business", emoji: "💼", label: "settings.docVisibility.options.business", desc: "settings.docVisibility.options.businessDesc" },
  { value: "shared",   emoji: "👥", label: "settings.docVisibility.options.shared",   desc: "settings.docVisibility.options.sharedDesc" },
];

function DefaultDocVisibilityChips({ toast }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
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
      toast(t("settings.docVisibility.saved"), "success");
    } catch (e: any) {
      setValue(prev);
      toast(e.message || t("settings.docVisibility.saveFailed"), "error");
    } finally { setSaving(false); }
  }

  return (
    <Card title={t("settings.docVisibility.title")}>
      <p className="text-xs text-muted-foreground mb-3">
        {t("settings.docVisibility.explain")}
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
              <span>{opt.emoji}</span> {t(opt.label)}
            </div>
            <div className="text-xs text-muted-foreground mt-0.5">{t(opt.desc)}</div>
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
  const { t } = useTranslation();
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
      toast(t("settings.signature.wrongType"), "error");
      return;
    }
    if (f.size > MAX_BYTES) {
      toast(t("settings.signature.tooLarge", { kb: Math.round(f.size / 1024) }), "error");
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
          reader.onerror = () => reject(new Error(t("settings.signature.readFailed")));
          reader.readAsDataURL(f);
        });
      }
      await api.patch("/api/profile", { signature_data_url: url });
      setDataUrl(url);
      await auth.refresh();
      toast(t("settings.signature.uploaded"), "success");
    } catch (err: any) {
      toast(t("settings.signature.saveFailed", { msg: err.message || err }), "error");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!confirm(t("settings.signature.removeConfirm"))) return;
    setSaving(true);
    try {
      await api.patch("/api/profile", { signature_data_url: null });
      setDataUrl("");
      await auth.refresh();
      toast(t("settings.signature.removed"), "success");
    } catch (err: any) {
      toast(t("settings.signature.removeFailed", { msg: err.message || err }), "error");
    } finally { setSaving(false); }
  }

  return (
    <Card title={t("settings.signature.title")}>
      <p className="text-xs text-muted-foreground mb-3">
        {t("settings.signature.explain")}
      </p>
      {dataUrl ? (
        <div className="flex items-start gap-4">
          <div className="border border-border rounded-md p-2 bg-white">
            <img
              src={dataUrl}
              alt={t("settings.signature.alt")}
              style={{ maxWidth: 240, maxHeight: 80, display: "block" }}
            />
          </div>
          <div className="flex flex-col gap-2">
            <button
              onClick={() => fileRef.current?.click()}
              disabled={saving}
              className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted disabled:opacity-50"
            >
              {t("settings.signature.replace")}
            </button>
            <button
              onClick={remove}
              disabled={saving}
              className="text-xs px-3 py-1.5 rounded-md border border-border text-red-600 hover:bg-red-500/10 disabled:opacity-50"
            >
              {t("settings.signature.remove")}
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
          {t("settings.signature.upload")}
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
  const { t } = useTranslation();
  return (
    <div className="space-y-4">
      <Card title={t("settings.storageTab.title")}>
        <p className="text-xs text-muted-foreground mb-4 leading-relaxed">
          {t("settings.storageTab.explain")}
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

type SearchIndexStatus = {
  enabled: boolean;
  embedder: "service" | "bundled";
  model: string;
  service: { configured: boolean; model: string; reachable: boolean | null; state?: string };
  schedule: "continuous" | "hourly" | "nightly";
  at: string;
  keep: "while_used" | "always";
  next_run: string | null;
  pending: number;
  sources: { source: string; indexed: number; total: number }[];
};

// i18n keys, resolved with t() at render time.
const SEARCH_SOURCE_LABEL: Record<string, string> = {
  email: "settings.searchMeaning.sources.email", whatsapp: "settings.searchMeaning.sources.whatsapp",
  tasks: "settings.searchMeaning.sources.tasks", contacts: "settings.searchMeaning.sources.contacts",
  events: "settings.searchMeaning.sources.events", recordings: "settings.searchMeaning.sources.recordings",
  drafts: "settings.searchMeaning.sources.drafts", bank: "settings.searchMeaning.sources.bank",
  letters: "settings.searchMeaning.sources.letters", paperless: "settings.searchMeaning.sources.paperless",
};

/** Settings → Embeddings: search by meaning on/off, which model, how
 *  far the index is. Backend: backend/search_index.py. */
function SearchByMeaningCard({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  const [st, setSt] = useState<SearchIndexStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setSt(await api.get<SearchIndexStatus>("/api/search/index")); } catch { /* card stays hidden */ }
  }, []);
  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [load]);

  async function put(body: { enabled?: boolean; embedder?: "service" | "bundled"; schedule?: string; at?: string; keep?: string }) {
    setBusy(true);
    try {
      setSt(await api.put<SearchIndexStatus>("/api/search/index", body));
    } catch (e: any) {
      toast(t("settings.searchMeaning.saveFailed", { msg: e?.message || e }), "error");
    } finally { setBusy(false); }
  }
  async function rebuild() {
    if (!confirm(t("settings.searchMeaning.rebuildConfirm"))) return;
    setBusy(true);
    try {
      await api.post("/api/search/index/rebuild", {});
      toast(t("settings.searchMeaning.rebuildStarted"), "success");
      load();
    } catch (e: any) {
      toast(t("settings.searchMeaning.rebuildFailed", { msg: e?.message || e }), "error");
    } finally { setBusy(false); }
  }

  if (!st) return null;
  const indexed = st.sources.reduce((n, x) => n + x.indexed, 0);
  const total = st.sources.reduce((n, x) => n + x.total, 0);
  const pct = total > 0 ? Math.min(100, Math.round((indexed / total) * 100)) : 0;

  return (
    <Card title={t("settings.searchMeaning.title")}>
      <div className="flex items-start justify-between gap-4">
        <p className="text-xs text-muted-foreground">
          {t("settings.searchMeaning.explain")}
        </p>
        <label className="flex items-center gap-2 text-sm shrink-0">
          <input
            type="checkbox"
            checked={st.enabled}
            disabled={busy}
            onChange={e => put({ enabled: e.target.checked })}
            className="w-4 h-4 accent-violet-500"
          />
          {st.enabled ? t("settings.searchMeaning.on") : t("settings.searchMeaning.off")}
        </label>
      </div>

      <div className={cn("mt-4", !st.enabled && "opacity-50")}>
        <div className="text-xs font-medium mb-1.5">{t("settings.searchMeaning.model")}</div>
        <div className="flex flex-col gap-1.5 text-sm">
          <label className="flex items-start gap-2">
            <input type="radio" name="search-embedder" className="mt-1 accent-violet-500"
              checked={st.embedder === "bundled"} disabled={busy || !st.enabled}
              onChange={() => put({ embedder: "bundled" })} />
            <span>{t("settings.searchMeaning.bundled")}
              <span className="block text-xs text-muted-foreground">{t("settings.searchMeaning.bundledHint")}</span>
            </span>
          </label>
          <label className={cn("flex items-start gap-2", !st.service.configured && "opacity-60")}>
            <input type="radio" name="search-embedder" className="mt-1 accent-violet-500"
              checked={st.embedder === "service"} disabled={busy || !st.enabled || !st.service.configured}
              onChange={() => put({ embedder: "service" })} />
            <span>{st.service.configured ? st.service.model : "Qwen3-Embedding-4B"} {t("settings.searchMeaning.serviceSuffix")}
              <span className="block text-xs text-muted-foreground">
                {!st.service.configured
                  ? t("settings.searchMeaning.serviceNotInstalled")
                  : st.service.state === "running"
                    ? t("settings.searchMeaning.serviceRunning")
                    : st.service.state === "starting"
                      ? t("settings.searchMeaning.serviceStarting")
                      : st.service.state === "stopped"
                        ? t("settings.searchMeaning.serviceAsleep")
                        : t("settings.searchMeaning.serviceUnreachable")}
              </span>
            </span>
          </label>
        </div>
        <p className="text-xs text-muted-foreground mt-2">{t("settings.searchMeaning.modelChangeNote")}</p>

        {/* When the index runs, and whether the model stays in memory
            (Dirk 2026-10-07: the service must not hold RAM around the
            clock on small PCs). */}
        <div className="mt-4 text-xs font-medium mb-1.5">{t("settings.searchMeaning.whenHeading")}</div>
        <div className="flex flex-col gap-1.5 text-sm">
          {([
            ["nightly", t("settings.searchMeaning.schedule.nightly"), t("settings.searchMeaning.schedule.nightlyHint")],
            ["hourly", t("settings.searchMeaning.schedule.hourly"), t("settings.searchMeaning.schedule.hourlyHint")],
            ["continuous", t("settings.searchMeaning.schedule.continuous"), t("settings.searchMeaning.schedule.continuousHint")],
          ] as const).map(([key, label, hint]) => (
            <label key={key} className="flex items-start gap-2">
              <input type="radio" name="search-schedule" className="mt-1 accent-violet-500"
                checked={st.schedule === key} disabled={busy || !st.enabled}
                onChange={() => put({ schedule: key })} />
              <span>{label}
                {key === "nightly" && (
                  <input type="time" value={st.at} disabled={busy || !st.enabled || st.schedule !== "nightly"}
                         onChange={e => e.target.value && put({ at: e.target.value })}
                         className="ml-2 h-6 px-1.5 rounded border border-border bg-background text-xs" />
                )}
                <span className="block text-xs text-muted-foreground">{hint}</span>
              </span>
            </label>
          ))}
        </div>
        {st.embedder === "service" && (
          <label className="mt-3 flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1 accent-violet-500" checked={st.keep === "always"} disabled={busy || !st.enabled}
                   onChange={e => put({ keep: e.target.checked ? "always" : "while_used" })} />
            <span>{t("settings.searchMeaning.keepLoaded")}
              <span className="block text-xs text-muted-foreground">
                {t("settings.searchMeaning.keepLoadedHint")}
              </span>
            </span>
          </label>
        )}
        <div className="mt-2 flex items-center gap-3 text-xs text-muted-foreground">
          {st.next_run && <span>{t("settings.searchMeaning.nextRun")} {st.next_run.slice(11, 16)}{st.next_run.slice(0, 10) !== new Date().toISOString().slice(0, 10) ? ` (${st.next_run.slice(8, 10)}.${st.next_run.slice(5, 7)}.)` : ""}</span>}
          {st.pending > 0 && <span>{t("settings.searchMeaning.waiting", { n: st.pending.toLocaleString(locale()) })}</span>}
          <button onClick={async () => { try { await api.post("/api/search/index/run", {}); toast(t("settings.searchMeaning.indexingNow"), "success"); } catch (e: any) { toast(e?.message || t("settings.searchMeaning.failed"), "error"); } }}
                  disabled={busy || !st.enabled}
                  className="px-2 py-0.5 rounded border border-border hover:bg-muted disabled:opacity-50">{t("settings.searchMeaning.indexNow")}</button>
        </div>

        <div className="mt-4 flex items-baseline justify-between">
          <div className="text-sm font-medium tabular-nums">
            {indexed.toLocaleString(locale())} <span className="text-muted-foreground font-normal">/ {total.toLocaleString(locale())} {t("settings.searchMeaning.entriesIndexed")}</span>
          </div>
          <div className="text-xs text-muted-foreground tabular-nums">{st.model} · {pct}%</div>
        </div>
        <div className="h-2 mt-1.5 rounded-full bg-muted overflow-hidden">
          <div className={cn("h-full rounded-full transition-all", pct === 100 ? "bg-emerald-500" : "bg-violet-500")}
               style={{ width: `${pct}%` }} />
        </div>
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-muted-foreground tabular-nums">
          {st.sources.map(x => (
            <span key={x.source}>{SEARCH_SOURCE_LABEL[x.source] ? t(SEARCH_SOURCE_LABEL[x.source]) : x.source} {x.indexed.toLocaleString(locale())}/{x.total.toLocaleString(locale())}</span>
          ))}
        </div>
        <button
          onClick={rebuild}
          disabled={busy || !st.enabled}
          className="mt-3 text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-50"
        >
          {t("settings.searchMeaning.rebuildIndex")}
        </button>
      </div>
    </Card>
  );
}

function EmbeddingsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<EmbeddingsStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [reindexing, setReindexing] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const s = await api.get<EmbeddingsStatus>("/api/embeddings/status");
      setStatus(s);
    } catch (e: any) {
      toast(t("settings.embeddings.loadFailed", { msg: e.message }), "error");
    } finally {
      setLoading(false);
    }
  }, [toast, t]);

  useEffect(() => {
    refresh();
    // Poll every 5s so the user watching a reindex job sees progress.
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function reindexAll() {
    if (!confirm(t("settings.embeddings.reindexConfirm"))) return;
    setReindexing(true);
    try {
      await api.post("/api/paperless/reindex-all", {});
      toast(t("settings.embeddings.reindexQueued"), "success");
      // Immediate refresh, then the 5s poll picks up the rest.
      refresh();
    } catch (e: any) {
      toast(t("settings.embeddings.reindexFailed", { msg: e.message }), "error");
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
      toast(t("settings.embeddings.stopRequested"), "info");
      refresh();
    } catch (e: any) {
      toast(t("settings.embeddings.stopFailed", { msg: e.message }), "error");
    } finally {
      setStopping(false);
    }
  }
  async function autotagAll(force: boolean) {
    if (!confirm(
      (force ? t("settings.embeddings.autotagConfirmForce") : t("settings.embeddings.autotagConfirm")) + "\n\n" +
      t("settings.embeddings.autotagConfirmBody")
    )) return;
    setAutotagging(true);
    try {
      await api.post("/api/embeddings/autotag-all", { force_retag: force });
      toast(force ? t("settings.embeddings.autotagQueuedForce") : t("settings.embeddings.autotagQueued"), "success");
      refresh();
    } catch (e: any) {
      toast(t("settings.embeddings.autotagFailed", { msg: e.message }), "error");
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
        {t("settings.embeddings.statusLoadFailed")} <button onClick={refresh} className="underline">{t("settings.embeddings.retry")}</button>
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
          <h2 className="text-lg font-semibold">{t("settings.embeddings.title")}</h2>
          <p className="text-sm text-muted-foreground mt-1">
            {t("settings.embeddings.subtitle")}
          </p>
        </div>
        <button
          onClick={refresh}
          className="text-xs px-3 py-1.5 rounded-md border border-border hover:bg-muted text-muted-foreground hover:text-foreground transition inline-flex items-center gap-1.5"
          title={t("settings.embeddings.refresh")}
        >
          <RefreshCw className={cn("w-3.5 h-3.5", loading && "animate-spin")} />
          {t("settings.embeddings.refresh")}
        </button>
      </header>

      <div className="space-y-4">

        <SearchByMeaningCard toast={toast} />

        <details className="group rounded-xl border border-border bg-card/40">
          <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium flex items-center gap-2">
            <ChevronRight className="w-4 h-4 transition group-open:rotate-90" />
            {t("settings.embeddings.detailsSummary")}
            <span className="text-xs font-normal text-muted-foreground">{t("settings.embeddings.detailsHint")}</span>
          </summary>
          <div className="px-4 pb-4 space-y-4">

        {/* Index population */}
        <Card title={t("settings.embeddings.populationTitle")}>
          <div className="flex items-baseline justify-between mb-2">
            <div className="text-2xl font-semibold tabular-nums">
              {status.vec_count.toLocaleString(locale())}
              <span className="text-muted-foreground text-base font-normal"> / {status.chunk_count.toLocaleString(locale())} {t("settings.embeddings.chunksEmbedded")}</span>
            </div>
            <div className="text-xs text-muted-foreground tabular-nums">
              {t("settings.embeddings.documents", { count: status.doc_count, n: status.doc_count.toLocaleString(locale()) })} · {pct}%
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
              {t("settings.embeddings.backlog", { n: backlog.toLocaleString(locale()) })}
              {recon?.status === "ok" && ` ${t("settings.embeddings.reconcilerCatchingUp")}`}
              {!reachable && ` ${t("settings.embeddings.embedderUnreachableBacklog")}`}
            </p>
          )}
          {backlog === 0 && status.chunk_count > 0 && (
            <p className="text-xs text-emerald-600 mt-2 inline-flex items-center gap-1.5">
              <CheckCircle2 className="w-3.5 h-3.5" /> {t("settings.embeddings.fullyEmbedded")}
            </p>
          )}
          {status.chunk_count === 0 && (
            <p className="text-xs text-amber-600 mt-2 inline-flex items-center gap-1.5">
              <AlertCircle className="w-3.5 h-3.5" />
              {t("settings.embeddings.noChunks")}
            </p>
          )}
        </Card>

        {/* Embedder status */}
        <Card title={t("settings.embeddings.embedderTitle")}>
          <div className="grid grid-cols-[120px_1fr] gap-y-2 text-sm">
            <div className="text-muted-foreground">{t("settings.embeddings.reachable")}</div>
            <div className="inline-flex items-center gap-1.5">
              {reachable ? (
                <><CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" /><span className="text-emerald-600 font-medium">{t("settings.embeddings.yes")}</span></>
              ) : (
                <><AlertCircle className="w-3.5 h-3.5 text-amber-500" /><span className="text-amber-600 font-medium">{t("settings.embeddings.no")}</span></>
              )}
            </div>
            <div className="text-muted-foreground">{t("settings.embeddings.backend")}</div>
            <div className="font-mono text-xs">{status.embedder.backend}</div>
            <div className="text-muted-foreground">{t("settings.embeddings.localModel")}</div>
            <div className="font-mono text-xs break-all">{status.embedder.local_model}</div>
            {status.embedder.external_url && (
              <>
                <div className="text-muted-foreground">{t("settings.embeddings.externalUrl")}</div>
                <div className="font-mono text-xs break-all">{status.embedder.external_url}</div>
                <div className="text-muted-foreground">{t("settings.embeddings.externalModel")}</div>
                <div className="font-mono text-xs">{status.embedder.external_model}</div>
              </>
            )}
            <div className="text-muted-foreground">{t("settings.embeddings.dimension")}</div>
            <div className="font-mono text-xs">{status.embedder.dim}</div>
          </div>
          {!reachable && (
            <div className="mt-3 p-3 rounded-md bg-amber-500/10 border border-amber-500/30 text-xs text-amber-700 dark:text-amber-300">
              <div className="font-medium mb-1 inline-flex items-center gap-1.5">
                <AlertCircle className="w-3.5 h-3.5" /> {t("settings.embeddings.notReachableTitle")}
              </div>
              <p className="leading-relaxed">
                {t("settings.embeddings.notReachableOn")}<code className="font-mono">auto</code>/<code className="font-mono">local</code>{t("settings.embeddings.notReachableMeans")}<code className="font-mono"> sentence-transformers</code>{t("settings.embeddings.notReachableRun")}<code className="font-mono">venv/bin/pip install -r backend/requirements.txt</code>{t("settings.embeddings.notReachableAfter")}
              </p>
            </div>
          )}
        </Card>

        {/* Reconciler heartbeat */}
        <Card title={t("settings.embeddings.reconcilerTitle")}>
          {recon ? (
            <div className="grid grid-cols-[140px_1fr] gap-y-2 text-sm">
              <div className="text-muted-foreground">{t("settings.embeddings.status")}</div>
              <div className="inline-flex items-center gap-1.5">
                {recon.status === "ok" && <><CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" /><span className="text-emerald-600 font-medium">{t("settings.embeddings.healthy")}</span></>}
                {recon.status === "warn" && <><AlertCircle className="w-3.5 h-3.5 text-amber-500" /><span className="text-amber-600 font-medium">{t("settings.embeddings.warning")}</span></>}
                {recon.status === "error" && <><AlertCircle className="w-3.5 h-3.5 text-red-500" /><span className="text-red-600 font-medium">{t("settings.embeddings.error")}</span></>}
                {recon.status === "starting" && <><Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground" /><span className="text-muted-foreground">{t("settings.embeddings.starting")}</span></>}
              </div>
              <div className="text-muted-foreground">{t("settings.embeddings.lastHeartbeat")}</div>
              <div>{recon.last_heartbeat_age_s == null ? t("settings.embeddings.never") : formatAge(recon.last_heartbeat_age_s)}</div>
              <div className="text-muted-foreground">{t("settings.embeddings.detail")}</div>
              <div className="text-xs">{recon.detail || <span className="text-muted-foreground">—</span>}</div>
              <div className="text-muted-foreground">{t("settings.embeddings.uptime")}</div>
              <div className="text-xs">{formatAge(recon.uptime_s)}</div>
              {recon.error_count > 0 && (
                <>
                  <div className="text-muted-foreground">{t("settings.embeddings.errors")}</div>
                  <div className="text-red-600 font-medium">{recon.error_count}</div>
                </>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              {t("settings.embeddings.reconcilerNotYet")}
            </p>
          )}
          <p className="text-xs text-muted-foreground mt-3">
            {t("settings.embeddings.reconcilerExplain")}
          </p>
        </Card>

        {/* Manual reindex */}
        <Card title={t("settings.embeddings.reindexTitle")}>
          <p className="text-xs text-muted-foreground mb-4 leading-relaxed">
            {t("settings.embeddings.reindexExplain")}
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
            {reindexing ? t("settings.embeddings.queuing") : t("settings.embeddings.reindexAll")}
          </button>
          {!reachable && (
            <p className="text-xs text-amber-600 mt-2">{t("settings.embeddings.needsReachable")}</p>
          )}
        </Card>

        {/* Autotagger */}
        <Card title={t("settings.embeddings.autotaggerTitle")}>
          <p className="text-xs text-muted-foreground mb-3 leading-relaxed">
            {t("settings.embeddings.autotaggerExplain")}
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
                  {status.autotagger.last_heartbeat_age_s == null ? t("settings.embeddings.never") : formatAge(status.autotagger.last_heartbeat_age_s)}
                </span>
              </div>
              {status.autotagger.detail && (
                <div className="font-mono text-xs text-foreground/80">{status.autotagger.detail}</div>
              )}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground mb-3 italic">
              {t("settings.embeddings.autotaggerNotYet")}
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
              {autotagging ? t("settings.embeddings.queuing") : t("settings.embeddings.autotagUntagged")}
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
              {t("settings.embeddings.retagAll")}
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
                {stopping ? t("settings.embeddings.stopping") : t("settings.embeddings.stop")}
              </button>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-2">
            {t("settings.embeddings.autotagTiming")}
          </p>
          <p className="text-xs text-muted-foreground mt-1 inline-flex items-start gap-1.5">
            <Info className="w-3 h-3 mt-0.5 shrink-0 opacity-70" />
            <span>
              {t("settings.embeddings.autotagServerNote")}
            </span>
          </p>
        </Card>

        {/* Tag distribution */}
        {status.taxonomy_tag_counts.length > 0 && (
          <Card title={t("settings.embeddings.tagDistTitle")}>
            <p className="text-xs text-muted-foreground mb-3 leading-relaxed">
              {t("settings.embeddings.tagDistExplain", { count: status.taxonomy_tag_counts.length })}
            </p>
            <div className="flex flex-wrap gap-1.5">
              {status.taxonomy_tag_counts.slice(0, 80).map(tc => (
                <span
                  key={tc.id}
                  title={t("settings.embeddings.tagTitle", { en: tc.label_en, de: tc.label_de, category: tc.category_id })}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-amber-500/10 text-amber-700 dark:text-amber-300 border border-amber-500/20"
                >
                  <span>{locale().startsWith("de") ? tc.label_de : tc.label_en}</span>
                  <span className="opacity-60">·</span>
                  <span className="tabular-nums">{tc.count}</span>
                </span>
              ))}
            </div>
            {status.taxonomy_tag_counts.length > 80 && (
              <p className="text-xs text-muted-foreground mt-2">
                {t("settings.embeddings.moreNotShown", { n: status.taxonomy_tag_counts.length - 80 })}
              </p>
            )}
          </Card>
        )}
          </div>
        </details>

      </div>
    </div>
  );
}

function formatAge(seconds: number): string {
  if (seconds < 60)   return i18n.t("settings.ageFmt.seconds", { n: seconds });
  if (seconds < 3600) return i18n.t("settings.ageFmt.minutes", { n: Math.floor(seconds / 60) });
  if (seconds < 86400) return i18n.t("settings.ageFmt.hoursMinutes", { h: Math.floor(seconds / 3600), m: Math.floor((seconds % 3600) / 60) });
  return i18n.t("settings.ageFmt.days", { n: Math.floor(seconds / 86400) });
}


// ─── Backup tab ──────────────────────────────────────────────────────

/** The easy first-time setup while backups aren't on yet; once they
 *  are, only the status and the details (running it again would quietly
 *  replace the passphrase older copies need). */
function BackupSetup() {
  const [configured, setConfigured] = useState<boolean | null>(null);
  // Bumped when the easy setup saved: the details below reload, while
  // the setup stays on screen to show the passphrase to write down.
  const [version, setVersion] = useState(0);
  useEffect(() => {
    api.get<{ config: { passphrase_set: boolean; schedule: string } }>("/api/backup/status")
      .then(r => setConfigured(!!(r.config?.passphrase_set && r.config?.schedule)))
      .catch(() => setConfigured(false));
  }, []);
  if (configured === null) return null;
  return (
    <>
      {!configured && <div className="mb-6"><EasyBackup onDone={() => setVersion(v => v + 1)} /></div>}
      <BackupPicker key={version} />
    </>
  );
}

function BackupTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-4">
      <Card title={t("settings.backupTab.title")}>
        <p className="text-xs text-muted-foreground mb-4 leading-relaxed">
          {t("settings.backupTab.intro")}
        </p>
        <BackupSetup />
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
  const { t } = useTranslation();
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
      toast(t("settings.llm.loadFailed", { msg: e.message }), "error");
    } finally {
      setLoading(false);
    }
  }, [toast, t]);

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
      toast(t("settings.llm.detectFailed", { msg: e.message }), "error");
    } finally {
      setDetecting(false);
    }
  }

  async function save() {
    if (!draftUrl.trim() || !draftModel.trim()) {
      toast(t("settings.llm.urlModelRequired"), "error");
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
      toast(t("settings.llm.saved"), "success");
    } catch (e: any) {
      toast(e.message || t("settings.llm.saveFailed"), "error");
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
        <h1 className="text-2xl font-semibold">{t("settings.llm.title")}</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t("settings.llm.intro")}
        </p>
      </header>

      {justConnected && cfg?.reachable && (
        <div className="mb-6 rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-4 py-3">
          <div className="flex items-start gap-3">
            <CheckCircle2 className="w-5 h-5 text-emerald-600 mt-0.5 shrink-0" />
            <div className="flex-1">
              <div className="text-sm font-semibold text-emerald-700 dark:text-emerald-300">
                {t("settings.llm.connected")}
              </div>
              <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
                {t("settings.llm.connectedAskBefore")} <em>{t("settings.llm.connectedAskQuestion")}</em> {t("settings.llm.connectedAskAfter")}
              </p>
              <div className="mt-3 flex items-center gap-2 flex-wrap">
                <a
                  href="/r/chat"
                  className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-700 dark:text-emerald-300 transition"
                >
                  {t("settings.llm.openChat")}
                </a>
                <button
                  onClick={() => setJustConnected(false)}
                  className="text-xs text-muted-foreground hover:text-foreground transition"
                >
                  {t("settings.llm.dismiss")}
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
          <Card title={t("settings.llm.chatModel")}>
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-sm">
                  {cfg.reachable
                    ? t("settings.llm.modelRunning")
                    : t("settings.llm.modelUnreachable")}
                </div>
                <div className="text-xs text-muted-foreground mt-2">{t("settings.llm.model")}</div>
                <div className="font-mono text-sm truncate">{cfg.model}</div>
                <div className="text-xs text-muted-foreground mt-2">{t("settings.llm.address")}</div>
                <div className="font-mono text-xs truncate text-muted-foreground">{cfg.base_url}</div>
              </div>
              <div className="shrink-0 text-right flex flex-col items-end gap-0.5">
                {cfg.reachable
                  ? <span className="inline-flex items-center gap-1 text-emerald-600 text-xs"><CheckCircle2 className="w-3.5 h-3.5" /> {t("settings.llm.reachable")}</span>
                  : <span className="inline-flex items-center gap-1 text-red-500 text-xs"><AlertCircle className="w-3.5 h-3.5" /> {t("settings.llm.offline")}</span>}
                {!cfg.reachable && cfg.reason && (
                  <div className="text-2xs text-muted-foreground mt-0.5">{cfg.reason}</div>
                )}
                <button
                  onClick={refresh}
                  className="text-2xs text-muted-foreground hover:text-foreground transition mt-1 inline-flex items-center gap-1"
                >
                  <RefreshCw className="w-2.5 h-2.5" /> {t("settings.llm.recheck")}
                </button>
              </div>
            </div>
          </Card>

          <details className="group rounded-xl border border-border bg-card/40" open={!cfg.reachable || undefined}>
            <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium flex items-center gap-2">
              <ChevronRight className="w-4 h-4 transition group-open:rotate-90" />
              {t("settings.llm.useDifferent")}
              <span className="text-xs font-normal text-muted-foreground">{t("settings.llm.useDifferentHint")}</span>
            </summary>
            <div className="px-4 pb-4 space-y-5">
          <p className="text-xs text-muted-foreground">
            {t("settings.llm.anyServerBefore")} (<code className="font-mono">curl -fsSL https://ollama.com/install.sh | sh</code>),{" "}
            {t("settings.llm.anyServerThen")} <code className="font-mono">ollama pull</code> {t("settings.llm.anyServerModel")} <strong>{t("settings.llm.scanNow")}</strong>.
          </p>
          <Card title={t("settings.llm.findServer")}>
            <p className="text-xs text-muted-foreground mb-3">
              {t("settings.llm.findServerIntro")}
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
              {t("settings.llm.scanNow")}
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
                        ? <span className="text-2xs text-emerald-600">{t("settings.llm.modelCount", { count: c.models.length })}</span>
                        : <span className="text-2xs text-muted-foreground">{c.reason}</span>}
                    </div>
                    {c.ok && c.models.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-2">
                        {c.models.map(m => (
                          <button
                            key={m}
                            onClick={() => useCandidate(c, m)}
                            className="text-2xs px-1.5 py-0.5 rounded bg-card border border-border hover:border-blue-500/40 font-mono"
                            title={t("settings.llm.useVia", { model: m, label: c.label })}
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

          <Card title={t("settings.llm.enterAddress")}>
            <Field label={t("settings.llm.serverAddress")}>
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
                  {t("settings.llm.test")}
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
                    ? t("settings.llm.probeOk", { count: probeResult.models.length })
                    : t("settings.llm.probeFailed", { reason: probeResult.reason })}
                </span>
              </div>
            )}

            <div className="mt-4">
              <Field label={t("settings.llm.model")}>
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
                      <option value={draftModel}>{draftModel} {t("settings.llm.notInList")}</option>
                    )}
                  </select>
                ) : (
                  <input
                    value={draftModel}
                    onChange={e => setDraftModel(e.target.value)}
                    placeholder={t("settings.llm.modelPlaceholder")}
                    className={cn(inputClass, "font-mono")}
                  />
                )}
              </Field>
              <div className="text-2xs text-muted-foreground mt-1">
                {t("settings.llm.modelHint")}
              </div>
            </div>

            <div className="mt-4">
              <Field label={t("settings.llm.apiKey")}>
                <div className="flex gap-2">
                  <input
                    type={showApiKey ? "text" : "password"}
                    value={draftApiKey ?? ""}
                    onChange={e => setDraftApiKey(e.target.value)}
                    placeholder={cfg.has_api_key ? t("settings.llm.apiKeyStored") : t("settings.llm.apiKeyEmpty")}
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
                    {showApiKey ? t("settings.llm.hide") : t("settings.llm.show")}
                  </button>
                  {cfg.has_api_key && draftApiKey !== "" && (
                    <button
                      type="button"
                      onClick={() => setDraftApiKey("")}
                      title={t("settings.llm.clearKeyTitle")}
                      className={cn(
                        "px-3 py-1.5 text-xs rounded-md font-medium transition shrink-0",
                        "bg-red-500/10 hover:bg-red-500/20 text-red-600 border border-red-500/20",
                      )}
                    >
                      {t("settings.llm.clear")}
                    </button>
                  )}
                </div>
              </Field>
              {cfg.has_api_key && draftApiKey === "" && (
                <div className="text-2xs text-amber-600 mt-1">{t("settings.llm.keyWillBeRemoved")}</div>
              )}
              <div className="text-2xs text-muted-foreground mt-1">
                {t("settings.llm.keyHintBefore")} <strong>{t("settings.llm.keyHintStrong")}</strong>{t("settings.llm.keyHintAfter")}
              </div>
            </div>

            {(() => {
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
                      <span>{t("settings.llm.runTestBefore")} <strong>{t("settings.llm.test")}</strong> {t("settings.llm.runTestAfter")}</span>
                    </div>
                  )}
                  {probeFailed && !unchanged && (
                    <div className="mt-4 text-xs text-red-600 dark:text-red-400 flex items-start gap-2">
                      <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                      <span>{t("settings.llm.noResponse")}</span>
                    </div>
                  )}
                  {probeResult?.ok && !probeResult.models.includes(draftModel) && !unchanged && (
                    <div className="mt-4 text-xs text-amber-600 dark:text-amber-400 flex items-start gap-2">
                      <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                      <span>{t("settings.llm.notServedBefore")} <code className="font-mono">{draftModel}</code>. {t("settings.llm.notServedAfter")}</span>
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
                      {t("settings.llm.saveApply")}
                    </button>
                  </div>
                </>
              );
            })()}
          </Card>
            </div>
          </details>

          <STTConfigCard toast={toast} />
        </div>
      )}
    </div>
  );
}


function NumberingTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  return (
    <div>
      <header className="mb-6">
        <h2 className="text-lg font-semibold">{t("settings.numbering.title")}</h2>
        <p className="text-sm text-muted-foreground mt-1">
          {t("settings.numbering.intro")}
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

// Backend ui_category buckets, in display order (System last).
const SKILL_CATEGORIES = [
  "Calendar", "Tasks", "Bills", "Contacts", "Documents",
  "Photos", "Email", "WhatsApp", "Compose", "System",
];

function SkillsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  // Category names come from the backend; the known buckets get a translated label.
  const catLabel = (c: string) => SKILL_CATEGORIES.includes(c) ? t(`settings.skills.categories.${c.toLowerCase()}`) : c;
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
      toast(t("settings.skills.loadFailed", { msg: e.message }), "error");
    }
  }, [toast, t]);

  useEffect(() => { refresh(); }, [refresh]);

  async function toggleSkill(name: string, nextEnabled: boolean) {
    setBusy(b => { const n = new Set(b); n.add(name); return n; });
    try {
      await api.patch(`/api/skills/${encodeURIComponent(name)}`, { enabled: nextEnabled });
      setSkills(curr => curr?.map(s => s.name === name ? { ...s, disabled: !nextEnabled } : s) || null);
    } catch (e: any) {
      toast(nextEnabled ? t("settings.skills.enableFailed", { name, msg: e.message }) : t("settings.skills.disableFailed", { name, msg: e.message }), "error");
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
        toast(nextEnabled ? t("settings.skills.catEnabled", { cat: catLabel(cat), count: r.flipped.length }) : t("settings.skills.catDisabled", { cat: catLabel(cat), count: r.flipped.length }), "success");
      }
    } catch (e: any) {
      toast(t("settings.skills.toggleFailed", { cat: catLabel(cat), msg: e.message }), "error");
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
  const CATEGORY_ORDER = SKILL_CATEGORIES;
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
        <h1 className="text-2xl font-semibold">{t("settings.skills.title")}</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t("settings.skills.intro", { enabled: totalEnabled, count: skills.length })}
        </p>
      </header>

      <Card title={t("settings.skills.ideaTitle")}>
        <p className="text-xs text-muted-foreground mb-2">
          {t("settings.skills.ideaBefore")} <code className="font-mono text-xs">backend/skills/</code>.{" "}
          {t("settings.skills.ideaMiddle")}{" "}
          <code className="font-mono text-xs">bash scripts/new-skill.sh &lt;name&gt;</code>{" "}
          {t("settings.skills.ideaAfter")}
        </p>
        <a
          href="https://github.com/winidi/yorik-ai/blob/main/docs/SKILLS.md"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-violet-500 hover:bg-violet-600 text-white font-medium transition"
        >
          <Lightbulb className="w-3.5 h-3.5" />
          {t("settings.skills.readGuide")}
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
                  <span className="font-semibold text-sm">{catLabel(cat)}</span>
                  <span className="text-2xs tabular-nums text-muted-foreground">
                    {enabledCount}/{members.length}
                  </span>
                  {!catFullyEnabled && catEnabled && (
                    <span className="text-2xs px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-600">
                      {t("settings.skills.partial")}
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
                  title={catEnabled ? t("settings.skills.disableAll", { cat: catLabel(cat) }) : t("settings.skills.enableAll", { cat: catLabel(cat) })}
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
                                  <span className="text-2xs px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-600">
                                    {t("settings.skills.mutates")}
                                  </span>
                                )}
                                {skill.tags?.includes("free") && (
                                  <span className="text-2xs px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-600">
                                    {t("settings.skills.free")}
                                  </span>
                                )}
                              </div>
                              <div className="flex items-center gap-3 text-2xs tabular-nums text-muted-foreground shrink-0">
                                {stat ? (
                                  <>
                                    <span>{t("settings.skills.calls", { count: stat.total })}</span>
                                    {successRate !== null && (
                                      <span className={cn(
                                        "font-medium",
                                        successRate >= 80 ? "text-emerald-600" :
                                        successRate >= 50 ? "text-amber-600" : "text-red-500",
                                      )}>{t("settings.skills.percentOk", { rate: successRate })}</span>
                                    )}
                                  </>
                                ) : (
                                  <span className="opacity-60">{t("settings.skills.neverCalled")}</span>
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
                            title={skillEnabled ? t("settings.skills.disableOne", { name: skill.name }) : t("settings.skills.enableOne", { name: skill.name })}
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
                                <div className="text-2xs text-muted-foreground font-semibold mb-0.5">{t("settings.skills.whenToUse")}</div>
                                <div className="whitespace-pre-wrap text-xs">{skill.when_to_use.trim()}</div>
                              </div>
                            )}
                            {skill.inputs && Object.keys(skill.inputs).length > 0 && (
                              <div>
                                <div className="text-2xs text-muted-foreground font-semibold mb-0.5">{t("settings.skills.inputs")}</div>
                                <div className="space-y-0.5">
                                  {Object.entries(skill.inputs).map(([key, spec]) => (
                                    <div key={key} className="text-xs">
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
                              <div className="text-xs">
                                <span className="text-muted-foreground">{t("settings.skills.allowedRoles")} </span>
                                <span className="font-mono">{skill.permissions.join(", ")}</span>
                              </div>
                            )}
                            {stat?.last_used && (
                              <div className="text-2xs text-muted-foreground">
                                {t("settings.skills.lastUsed", { date: new Date(stat.last_used).toLocaleString(locale()) })}
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
  const { t } = useTranslation();
  const [data, setData] = useState<QualitySummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(30);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.get<QualitySummary>(`/api/quality/summary?role=admin&days=${days}`);
      setData(r);
    } catch (e: any) {
      toast(t("settings.quality.loadFailed", { msg: e.message }), "error");
    } finally {
      setLoading(false);
    }
  }, [days, toast, t]);

  useEffect(() => { refresh(); }, [refresh]);

  return (
    <div>
      <header className="mb-6 flex items-baseline justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("settings.quality.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {t("settings.quality.intro")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={days}
            onChange={e => setDays(Number(e.target.value))}
            className={cn(inputClass, "h-8 text-xs w-auto")}
          >
            <option value={7}>{t("settings.quality.lastDays", { count: 7 })}</option>
            <option value={30}>{t("settings.quality.lastDays", { count: 30 })}</option>
            <option value={90}>{t("settings.quality.lastDays", { count: 90 })}</option>
            <option value={365}>{t("settings.quality.lastYear")}</option>
          </select>
          <button
            onClick={refresh}
            className="w-8 h-8 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center justify-center"
            title={t("settings.quality.reload")}
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
          <Card title={t("settings.quality.currentLlm")}>
            <div className="flex items-center gap-3 py-1">
              <div className="w-10 h-10 rounded-md bg-gradient-to-br from-violet-500/30 to-blue-500/30 flex items-center justify-center">
                <Sparkles className="w-4 h-4 text-violet-500" />
              </div>
              <div className="font-mono text-sm">{data.current_model}</div>
              <div className="ml-auto text-xs text-muted-foreground">
                {t("settings.quality.changeModelHint")}
              </div>
            </div>
          </Card>

          <Card title={t("settings.quality.skillsTitle", { n: data.skills.length })}>
            {data.skills.length === 0 ? (
              <EmptyMetric icon={Database} label={t("settings.quality.noSkillCalls")} />
            ) : (
              <div className="divide-y divide-border">
                {data.skills.map((s, i) => {
                  const rate = s.n > 0 ? Math.round((s.successes / s.n) * 100) : 0;
                  const color = rate >= 80 ? "text-emerald-600" : rate >= 60 ? "text-amber-600" : "text-red-500";
                  return (
                    <div key={i} className="py-2.5 flex items-center gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium truncate">{s.skill_id}</div>
                        <div className="text-2xs text-muted-foreground font-mono">
                          {s.llm_model || t("settings.quality.noModelTag")}
                          {s.avg_latency_ms && ` · ~${Math.round(s.avg_latency_ms)}ms`}
                        </div>
                      </div>
                      <div className={cn("font-mono text-sm font-semibold", color)}>{rate}%</div>
                      <div className="text-xs text-muted-foreground tabular-nums w-16 text-right">
                        {s.successes}/{s.n}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>

          <Card title={t("settings.quality.turnsRated", { n: data.turns_by_model.reduce((a, turn) => a + turn.n, 0) })}>
            {data.turns_by_model.length === 0 ? (
              <EmptyMetric icon={ThumbsUp} label={t("settings.quality.noRatings")} />
            ) : (
              <div className="space-y-2">
                {data.turns_by_model.map((turn, i) => {
                  const total = turn.up + turn.down;
                  const rate = total > 0 ? Math.round((turn.up / total) * 100) : 0;
                  return (
                    <div key={i} className="flex items-center gap-3 text-sm">
                      <div className="font-mono flex-1 min-w-0 truncate">{turn.llm_model || t("settings.quality.noModel")}</div>
                      <span className="inline-flex items-center gap-0.5 text-emerald-500"><ThumbsUp className="w-3 h-3" />{turn.up}</span>
                      <span className="inline-flex items-center gap-0.5 text-red-500"><ThumbsDown className="w-3 h-3" />{turn.down}</span>
                      <span className="font-mono text-foreground/80 w-12 text-right">{rate}%</span>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>

          {data.recent_failures.length > 0 && (
            <Card title={t("settings.quality.recentFailures")}>
              <div className="space-y-1.5">
                {data.recent_failures.map((f, i) => (
                  <div key={i} className="text-xs bg-red-500/5 border border-red-500/15 rounded-md p-2.5">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="font-mono font-medium text-red-600">{f.skill_id}</span>
                      <span className="text-2xs text-muted-foreground">{f.created_at}</span>
                    </div>
                    <div className="text-xs text-muted-foreground mt-0.5 font-mono leading-snug line-clamp-2">{f.error}</div>
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

// ─── Users tab ────────────────────────────────────────────────────────
// Admin-only CRUD over user_profiles. Backed by /api/users
// (backend/users.py). "Invite" is admin-creates-credentials + a
// copy-to-clipboard handoff block — no token-link / SMTP email flow
// yet; that's a bigger feature.

interface YorikUserRow {
  id: string;
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

// What the household picks from. The backend stores restricted for a
// child (child/employee/viewer are old names for the same thing, folded
// into restricted on save); platform_admin is the install's owner and
// can't be picked here.
const ROLES = ["member", "restricted", "admin"] as const;
type Role = typeof ROLES[number];
// Translation keys; resolve with t(ROLE_LABEL[r]) at render time.
const ROLE_LABEL: Record<string, string> = {
  member: "settings.users.roles.adult", restricted: "settings.users.roles.child", admin: "settings.users.roles.admin", platform_admin: "settings.users.roles.owner",
  child: "settings.users.roles.child", employee: "settings.users.roles.child", viewer: "settings.users.roles.child",
};
const ROLE_HINT: Record<string, string> = {
  member: "settings.users.roleHints.adult",
  restricted: "settings.users.roleHints.child",
  admin: "settings.users.roleHints.admin",
};
/** QR-invited people get a placeholder address nobody reads. */
const isPlaceholderEmail = (e: string | null) => !!e && e.endsWith("@members.yorik.invalid");
const isChildRole = (r: string) => ["restricted", "child", "employee", "viewer"].includes(r);

function UsersTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  const auth = useAuth();
  const [users, setUsers] = useState<YorikUserRow[] | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [showInvite, setShowInvite] = useState(false);
  const [resetting, setResetting] = useState<YorikUserRow | null>(null);
  // A child's sharing is set by the parents (the backend allows admins
  // exactly this, for restricted accounts).
  const [sharingFor, setSharingFor] = useState<YorikUserRow | null>(null);
  const [credentialsHandoff, setCredentialsHandoff] = useState<{
    name: string; email: string; password: string;
  } | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await api.get<YorikUserRow[]>("/api/users");
      setUsers(list);
    } catch (e: any) {
      setUsers([]);
      if (e?.status !== 403) toast(t("settings.users.loadFailed"), "error");
    }
  }, [toast, t]);

  useEffect(() => { void load(); }, [load]);

  async function toggleDisabled(u: YorikUserRow) {
    try {
      await api.patch(`/api/users/${u.id}`, { disabled: !u.disabled });
      toast(u.disabled ? t("settings.users.canSignInAgain", { name: u.name }) : t("settings.users.pausedOut", { name: u.name }), "success");
      await load();
    } catch (e: any) { toast(e?.message || t("settings.users.failed"), "error"); }
  }

  async function changeRole(u: YorikUserRow, role: Role) {
    if (role === u.role) return;
    try {
      await api.patch(`/api/users/${u.id}`, { role });
      toast(t("settings.users.nowRole", { name: u.name, role: t(ROLE_LABEL[role]) }), "success");
      await load();
    } catch (e: any) { toast(e?.message || t("settings.users.failed"), "error"); }
  }

  async function deleteUser(u: YorikUserRow) {
    if (!confirm(t("settings.users.removeConfirm", { name: u.name }))) return;
    try {
      await api.delete(`/api/users/${u.id}`);
      toast(t("settings.users.deleted", { name: u.name }), "success");
      await load();
    } catch (e: any) { toast(e?.message || t("settings.users.failed"), "error"); }
  }

  return (
    <div>
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("settings.users.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {t("settings.users.intro")}
          </p>
        </div>
        <div className="flex flex-col sm:flex-row gap-2 shrink-0">
          <button
            onClick={() => setShowInvite(true)}
            className="px-3 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition inline-flex items-center gap-1.5"
          >
            <QrCode className="w-4 h-4" /> {t("settings.users.inviteQr")}
          </button>
          <button
            onClick={() => setShowAdd(true)}
            className="px-3 py-2 rounded-md border border-border text-sm font-medium hover:bg-muted transition inline-flex items-center gap-1.5"
          >
            <UserPlus className="w-4 h-4" /> {t("settings.users.addByHand")}
          </button>
        </div>
      </header>
      {showInvite && <InviteDialog onClose={() => { setShowInvite(false); load(); }} />}
      {sharingFor && (
        <div className="fixed inset-0 z-[850] flex items-start justify-center p-4 bg-black/50 backdrop-blur-sm overflow-y-auto" onClick={() => setSharingFor(null)}>
          <div className="w-full max-w-2xl my-8 bg-background border border-border rounded-2xl shadow-2xl p-5" onClick={e => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-3 mb-3">
              <div>
                <h2 className="text-lg font-semibold">{t("settings.users.sharingFor", { name: sharingFor.name })}</h2>
                <p className="text-xs text-muted-foreground mt-0.5">
                  {t("settings.users.sharingForIntro", { name: sharingFor.name })}
                </p>
              </div>
              <button onClick={() => setSharingFor(null)} className="p-1 text-muted-foreground hover:text-foreground" aria-label={t("settings.users.close")}>
                <X className="w-4 h-4" />
              </button>
            </div>
            <SharingCard toast={toast} ownerId={String(sharingFor.id)} title={t("settings.users.whatShares", { name: sharingFor.name })} />
          </div>
        </div>
      )}

      {users === null && (
        <div className="text-sm text-muted-foreground">{t("settings.users.loading")}</div>
      )}
      {users && users.length === 0 && (
        <div className="bg-card border border-border rounded-xl p-5 text-sm text-muted-foreground">
          {t("settings.users.adminRequired")}
        </div>
      )}

      {users && users.length > 0 && (
        <div className="space-y-2">
          {users.map(u => (
            <UserRow
              key={u.id}
              user={u}
              isSelf={String(u.id) === String(auth.user.id)}
              onToggleDisabled={() => toggleDisabled(u)}
              onChangeRole={(r) => changeRole(u, r)}
              onResetPassword={() => setResetting(u)}
              onSharing={isChildRole(u.role) ? () => setSharingFor(u) : undefined}
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

function UserRow({ user: u, isSelf, onToggleDisabled, onChangeRole, onResetPassword, onDelete, onSharing }: {
  user: YorikUserRow;
  isSelf: boolean;
  onToggleDisabled: () => void;
  onChangeRole: (role: Role) => void;
  onResetPassword: () => void;
  onDelete: () => void;
  /** Only for a child: open what they share and see. */
  onSharing?: () => void;
}) {
  const { t } = useTranslation();
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
            <span className="text-2xs px-1.5 py-0.5 rounded-full bg-primary/10 text-primary">
              {t("settings.users.you")}
            </span>
          )}
          {u.disabled && (
            <span className="text-2xs px-1.5 py-0.5 rounded-full bg-rose-500/10 text-rose-600 dark:text-rose-400">
              {t("settings.users.paused")}
            </span>
          )}
          {!u.has_password && (
            <span className="text-2xs px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-600 dark:text-amber-400">
              {t("settings.users.noPassword")}
            </span>
          )}
        </div>
        <div className="text-xs text-muted-foreground truncate mt-0.5">
          {isPlaceholderEmail(u.email) ? t("settings.users.pinSignIn") : (u.email || <em>{t("settings.users.noEmail")}</em>)}
          {u.last_login_at && <span className="ml-2 opacity-70">· {t("settings.users.lastLogin", { when: formatRelativeShort(u.last_login_at) })}</span>}
          {u.active_sessions > 0 && <span className="ml-2 opacity-70">· {t("settings.users.sessions", { count: u.active_sessions })}</span>}
        </div>
      </div>
      <select
        value={(ROLES as readonly string[]).includes(u.role) ? u.role : u.role === "platform_admin" ? "platform_admin" : "restricted"}
        onChange={e => onChangeRole(e.target.value as Role)}
        disabled={isSelf || u.role === "platform_admin"}
        className="h-8 px-2 rounded bg-muted text-xs focus:outline-none disabled:opacity-50"
        title={isSelf ? t("settings.users.ownRole") : u.role === "platform_admin" ? t("settings.users.ownerOfYorik") : ROLE_HINT[u.role] ? t(ROLE_HINT[u.role]) : t("settings.users.changeRole")}
      >
        {u.role === "platform_admin" && <option value="platform_admin">{t(ROLE_LABEL.platform_admin)}</option>}
        {ROLES.map(r => <option key={r} value={r}>{t(ROLE_LABEL[r])}</option>)}
      </select>
      {onSharing && (
        <button
          onClick={onSharing}
          title={t("settings.users.childSharingTitle")}
          className="h-8 px-2 rounded-md hover:bg-muted text-xs text-muted-foreground hover:text-foreground transition inline-flex items-center gap-1"
        >
          <Shield className="w-3.5 h-3.5" /> {t("settings.users.sharing")}
        </button>
      )}
      <button
        onClick={onResetPassword}
        title={t("settings.users.resetPassword")}
        className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition"
      >
        <KeyRound className="w-4 h-4" />
      </button>
      <button
        onClick={onToggleDisabled}
        disabled={isSelf}
        title={isSelf ? t("settings.users.cantPauseSelf") : (u.disabled ? t("settings.users.allowAgain") : t("settings.users.pauseAccess"))}
        className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-30 disabled:cursor-not-allowed"
      >
        <Power className="w-4 h-4" />
      </button>
      <button
        onClick={onDelete}
        disabled={isSelf}
        title={isSelf ? t("settings.users.cantRemoveSelf") : t("settings.users.removeFromYorik")}
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
  if (days < 1) return i18n.t("settings.users.rel.today");
  if (days === 1) return i18n.t("settings.users.rel.yesterday");
  if (days < 7) return i18n.t("settings.users.rel.daysAgo", { n: days });
  if (days < 30) return i18n.t("settings.users.rel.weeksAgo", { n: Math.floor(days / 7) });
  return d.toLocaleDateString(locale(), { month: "short", day: "numeric" });
}

function generatePassword(): string {
  // Easy-to-share but secure: three random Diceware-ish words + 3 digits.
  // No ambiguous chars in the digits. Avoids the "this looks scary" problem
  // of base64 random strings — the admin can read it over the phone if
  // they have to.
  const words = [
    "anchor", "beaver", "delta", "echo", "feather", "ghost", "harbor", "igloo",
    "jade", "kettle", "lemon", "maple", "nectar", "ocean", "pebble", "quartz",
    "raven", "sage", "tundra", "umber", "violet", "willow", "xenon", "yarrow", "zephyr",
    "amber", "birch", "cedar", "dune", "ember", "fjord", "glade",
  ];
  // crypto, not Math.random: these are real passwords.
  const rand = (n: number) => { const a = new Uint32Array(1); crypto.getRandomValues(a); return a[0] % n; };
  const pick = () => words[rand(words.length)];
  const digits = String(100 + rand(900));
  return `${pick()}-${pick()}-${pick()}-${digits}`;
}

function AddUserModal({ onClose, onCreated, toast }: {
  onClose: () => void;
  onCreated: (creds: { name: string; email: string; password: string }) => void;
  toast: (t: string, k?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
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
      toast(t("settings.users.add.added", { name }), "success");
      onCreated({ name, email, password });
    } catch (e: any) {
      toast(e?.message || t("settings.users.add.createFailed"), "error");
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
            <div className="font-semibold">{t("settings.users.add.title")}</div>
          </div>
          <button type="button" onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-3">
          <Field label={t("settings.users.add.name")}>
            <input
              required
              autoFocus
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder={t("settings.users.add.namePlaceholder")}
              className={inputClass}
            />
          </Field>
          <Field label={t("settings.users.add.email")}>
            <input
              required
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder={t("settings.users.add.emailPlaceholder")}
              className={inputClass}
            />
          </Field>
          <Field label={t("settings.users.add.role")}>
            <select value={role} onChange={e => setRole(e.target.value as Role)} className={inputClass}>
              {ROLES.map(r => <option key={r} value={r}>{t(ROLE_LABEL[r])}</option>)}
            </select>
            <p className="text-xs text-muted-foreground mt-1">{t(ROLE_HINT[role])}</p>
          </Field>
          <Field label={t("settings.users.add.initialPassword")}>
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
                title={t("settings.users.add.generateNew")}
                className="px-2 rounded-md bg-muted hover:bg-muted/80 text-xs text-muted-foreground shrink-0"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            </div>
            <div className="text-xs text-muted-foreground mt-1 leading-relaxed">
              {t("settings.users.add.passwordHint")}
            </div>
          </Field>
          <div className="border-t border-border pt-3 space-y-2">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                checked={provisionPaperless}
                onChange={e => setProvisionPaperless(e.target.checked)}
              />
              <span>{t("settings.users.add.paperless")}</span>
            </label>
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                checked={provisionImmich}
                onChange={e => setProvisionImmich(e.target.checked)}
              />
              <span>{t("settings.users.add.immich")}</span>
            </label>
            <div className="text-xs text-muted-foreground mt-1 leading-relaxed">
              {t("settings.users.add.provisionHint")}
            </div>
          </div>
        </div>
        <footer className="px-5 py-3 border-t border-border bg-muted/30 flex items-center justify-end gap-2">
          <button type="button" onClick={onClose} className="px-3 py-1.5 rounded-md text-sm hover:bg-muted transition">
            {t("settings.users.add.cancel")}
          </button>
          <button
            type="submit"
            disabled={busy || !name || !email || password.length < 8}
            className="px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition inline-flex items-center gap-1.5 disabled:opacity-50"
          >
            {busy ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <UserPlus className="w-3.5 h-3.5" />}
            {t("settings.users.add.submit")}
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
  const { t } = useTranslation();
  const [password, setPassword] = useState(() => generatePassword());
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.post(`/api/users/${user.id}/reset-password`, { new_password: password });
      toast(t("settings.users.reset.done", { name: user.name }), "success");
      onDone(password);
    } catch (e: any) {
      toast(e?.message || t("settings.users.reset.failed"), "error");
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
            <div className="font-semibold">{t("settings.users.reset.title", { name: user.name })}</div>
          </div>
          <button type="button" onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-3">
          <Field label={t("settings.users.reset.newPassword")}>
            <div className="flex gap-1.5">
              <input required minLength={8} value={password} onChange={e => setPassword(e.target.value)} className={inputClass} autoFocus />
              <button type="button" onClick={() => setPassword(generatePassword())} className="px-2 rounded-md bg-muted hover:bg-muted/80 text-xs text-muted-foreground shrink-0">
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            </div>
          </Field>
          <div className="text-xs text-muted-foreground leading-relaxed">
            {t("settings.users.reset.hint", { name: user.name })}
          </div>
        </div>
        <footer className="px-5 py-3 border-t border-border bg-muted/30 flex items-center justify-end gap-2">
          <button type="button" onClick={onClose} className="px-3 py-1.5 rounded-md text-sm hover:bg-muted transition">{t("settings.users.reset.cancel")}</button>
          <button type="submit" disabled={busy || password.length < 8}
            className="px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition disabled:opacity-50">
            {t("settings.users.reset.submit")}
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
  const { t } = useTranslation();
  const loginUrl = typeof window !== "undefined" ? `${window.location.origin}/` : "/";
  const block = t("settings.users.handoff.message", { name, loginUrl, email, password });

  async function copyAll() {
    try {
      await navigator.clipboard.writeText(block);
      toast(t("settings.users.handoff.copied"), "success");
    } catch {
      toast(t("settings.users.handoff.clipboardUnavailable"), "error");
    }
  }
  async function copyOne(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      toast(t("settings.users.handoff.oneCopied", { label }), "success");
    } catch {/* ignore */}
  }

  return createPortal(
    <div className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4" onClick={onClose}>
      <div className="w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden" onClick={e => e.stopPropagation()}>
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <CheckCircle2 className="w-4 h-4 text-emerald-500" />
            <div className="font-semibold">{t("settings.users.handoff.title", { name })}</div>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-3">
          <p className="text-sm text-muted-foreground leading-relaxed">
            {t("settings.users.handoff.intro", { name })}
          </p>
          <div className="bg-muted/50 rounded-lg p-3 text-sm font-mono space-y-1.5">
            <CredRow label={t("settings.users.handoff.url")}      value={loginUrl}  onCopy={() => copyOne(loginUrl, t("settings.users.handoff.url"))} />
            <CredRow label={t("settings.users.handoff.email")}    value={email}     onCopy={() => copyOne(email, t("settings.users.handoff.email"))} />
            <CredRow label={t("settings.users.handoff.password")} value={password}  onCopy={() => copyOne(password, t("settings.users.handoff.password"))} mono />
          </div>
          <button
            onClick={copyAll}
            className="w-full px-3 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 transition inline-flex items-center justify-center gap-1.5"
          >
            <Copy className="w-3.5 h-3.5" />
            {t("settings.users.handoff.copyMessage")}
          </button>
          <div className="text-xs text-muted-foreground leading-relaxed text-center">
            {t("settings.users.handoff.shownOnce")}
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
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-2">
      <span className="text-2xs text-muted-foreground font-semibold w-16 shrink-0">{label}</span>
      <span className={cn("flex-1 truncate", mono && "font-mono")}>{value}</span>
      <button onClick={onCopy} className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground transition" title={t("settings.users.handoff.copyLabel", { label })}>
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
  experimental?: boolean;
}

function AppsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
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
      if (e?.status && e.status !== 403) toast(t("settings.appsTab.loadFailed"), "error");
    }
  }, [toast, t]);

  useEffect(() => { void load(); }, [load]);

  async function toggle(appId: string, next: boolean) {
    setBusy(appId);
    try {
      await api.post(`/api/apps/${appId}/${next ? "enable" : "disable"}`);
      setApps(curr => curr?.map(a => a.id === appId ? { ...a, enabled: next } : a) ?? curr);
      window.dispatchEvent(new Event("yorik:apps-changed"));
      const name = apps?.find(a => a.id === appId)?.name || appId;
      toast(next ? t("settings.appsTab.turnedOn", { name }) : t("settings.appsTab.turnedOff", { name }), "success");
    } catch {
      toast(next ? t("settings.appsTab.enableFailed", { name: appId }) : t("settings.appsTab.disableFailed", { name: appId }), "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">{t("settings.appsTab.title")}</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t("settings.appsTab.intro")}
        </p>
      </header>

      {apps === null && (
        <div className="text-sm text-muted-foreground">{t("settings.appsTab.loading")}</div>
      )}

      {apps && apps.length === 0 && (
        <div className="bg-card border border-border rounded-xl p-5 text-sm text-muted-foreground">
          {t("settings.appsTab.none")}
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
                  <div className="font-semibold flex items-center gap-2 flex-wrap">
                    {a.name}
                    {a.experimental && (
                      <span
                        className="text-[10px] font-medium uppercase tracking-wide px-1.5 py-0.5 rounded border border-amber-500/40 text-amber-600 dark:text-amber-400"
                        title={t("settings.appsTab.experimentalTitle")}
                      >
                        {t("settings.appsTab.experimental")}
                      </span>
                    )}
                  </div>
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
                  aria-label={a.enabled ? t("settings.appsTab.disableApp", { name: a.name }) : t("settings.appsTab.enableApp", { name: a.name })}
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
  const { t } = useTranslation();
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
      if (e?.status && e.status !== 403) toast(t("settings.marketplace.loadFailed"), "error");
    }
  }, [toast, t]);

  useEffect(() => { void load(); }, [load]);

  async function doInstall(app: MarketplaceApp) {
    setPending(null);
    setBusy(app.id);
    try {
      await api.post(`/api/apps/install_from_catalog/${app.id}`);
      toast(t("settings.marketplace.installed", { name: app.name }), "success");
      await load();
    } catch {
      toast(t("settings.marketplace.installFailed", { name: app.name }), "error");
    } finally {
      setBusy(null);
    }
  }

  async function doUninstall(app: MarketplaceApp) {
    setPendingUninstall(null);
    setBusy(app.id);
    try {
      await api.delete(`/api/apps/${app.id}?wipe_data=true`);
      toast(t("settings.marketplace.uninstalled", { name: app.name }), "success");
      await load();
    } catch {
      toast(t("settings.marketplace.uninstallFailed", { name: app.name }), "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">{t("settings.marketplace.title")}</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t("settings.marketplace.intro")}
        </p>
      </header>

      {/* Safety intro — what the sandbox actually does. */}
      <div className="mb-5 rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4 flex gap-3">
        <Shield className="w-4 h-4 text-emerald-500 shrink-0 mt-0.5" />
        <div className="text-[13px] leading-relaxed">
          <div className="font-medium text-foreground">{t("settings.marketplace.trustTitle")}</div>
          <div className="text-muted-foreground mt-0.5">
            {t("settings.marketplace.trustBefore")}
            <span className="inline-flex items-center gap-1"><BadgeCheck className="w-3 h-3 text-emerald-600 inline" /> {t("settings.marketplace.verified")}</span>{t("settings.marketplace.trustAfter")}
          </div>
        </div>
      </div>

      {apps === null && (
        <div className="text-sm text-muted-foreground">{t("settings.marketplace.loading")}</div>
      )}

      {apps && apps.length === 0 && (
        <div className="bg-card border border-border rounded-xl p-8 text-sm text-muted-foreground text-center">
          <Store className="w-6 h-6 mx-auto mb-2 opacity-50" />
          <div>{t("settings.marketplace.emptyTitle")}</div>
          <div className="mt-1 text-xs">{t("settings.marketplace.emptyHint")}</div>
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
                      <span className="text-xs text-muted-foreground">{t("settings.marketplace.by", { author: a.author })}</span>
                    )}
                    {verified && (
                      <span
                        className="inline-flex items-center gap-1 text-2xs font-semibold px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                        title={t("settings.marketplace.verifiedTitle")}
                      >
                        <BadgeCheck className="w-3 h-3" />
                        {t("settings.marketplace.verified")}
                      </span>
                    )}
                  </div>
                  <div className="text-sm text-muted-foreground mt-1 leading-relaxed">
                    {a.description}
                  </div>
                  {a.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {a.tags.map(t => (
                        <span key={t} className="text-2xs px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
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
                      {t("settings.marketplace.source")} <ExternalLink className="w-3 h-3" />
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
                    {busy === a.id ? t("settings.marketplace.uninstalling") : t("settings.marketplace.uninstall")}
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
                    {busy === a.id ? t("settings.marketplace.installing") : t("settings.marketplace.install")}
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
  const { t } = useTranslation();
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
          <div className="font-semibold">{t("settings.marketplace.installModal.question", { name: app.name })}</div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground" aria-label={t("settings.marketplace.installModal.close")}>
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
                  <span className="inline-flex items-center gap-1 text-2xs font-semibold px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-700 dark:text-emerald-400">
                    <BadgeCheck className="w-3 h-3" />
                    {t("settings.marketplace.verified")}
                  </span>
                )}
              </div>
              {app.author && (
                <div className="text-xs text-muted-foreground mt-0.5">{t("settings.marketplace.by", { author: app.author })}</div>
              )}
              <p className="text-sm text-muted-foreground mt-2 leading-relaxed">{app.description}</p>
            </div>
          </div>

          <div className="border-t border-border pt-4">
            <div className="text-2xs font-semibold text-muted-foreground mb-2">
              {t("settings.marketplace.installModal.accessTitle")}
            </div>
            <ul className="space-y-1.5 text-[13px]">
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
                <span>
                  {t("settings.marketplace.installModal.ownDb")}
                  <span className="font-mono text-xs text-muted-foreground"> (data/apps/{app.id}/data.db)</span>
                </span>
              </li>
              {tables.length > 0 && tables.map((tb, i) => (
                <li key={i} className="flex items-start gap-2">
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
                  <span>
                    <strong>{tb.access || "read"}</strong> {t("settings.marketplace.installModal.accessTo")}
                    <span className="font-mono text-xs ml-1">{tb.db}.{tb.table}</span>
                  </span>
                </li>
              ))}
              {connectors.length > 0 && connectors.map((c, i) => (
                <li key={i} className="flex items-start gap-2">
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
                  <span>{t("settings.marketplace.installModal.callsConnectorBefore")}<span className="font-mono text-xs">{c}</span>{t("settings.marketplace.installModal.callsConnectorAfter")}</span>
                </li>
              ))}
              {selfContained && (
                <li className="flex items-start gap-2 text-muted-foreground">
                  <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
                  <span>{t("settings.marketplace.installModal.yorikLlm")}</span>
                </li>
              )}
            </ul>
          </div>

          <div className="border-t border-border pt-4">
            <div className="text-2xs font-semibold text-muted-foreground mb-2">
              {t("settings.marketplace.installModal.cannotTitle")}
            </div>
            <ul className="space-y-1 text-[13px] text-muted-foreground">
              <li className="flex items-center gap-2">
                <Globe className="w-3 h-3 shrink-0" />
                {t("settings.marketplace.installModal.noInternet")}
              </li>
              <li className="flex items-center gap-2">
                <Lock className="w-3 h-3 shrink-0" />
                {t("settings.marketplace.installModal.noSignIn")}
              </li>
            </ul>
            <p className="text-xs text-muted-foreground mt-2">
              {t("settings.marketplace.installModal.serverNote")}
            </p>
          </div>

          <div className="flex gap-2 pt-2">
            <button
              onClick={onClose}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium border border-border hover:bg-muted text-muted-foreground hover:text-foreground transition"
            >
              {t("settings.marketplace.installModal.cancel")}
            </button>
            <button
              onClick={onConfirm}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium bg-primary text-primary-foreground hover:bg-primary/90 transition inline-flex items-center justify-center gap-1.5"
            >
              <Download className="w-3.5 h-3.5" />
              {t("settings.marketplace.installModal.confirm", { name: app.name })}
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
  const { t } = useTranslation();
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
          <div className="font-semibold">{t("settings.marketplace.uninstallModal.question", { name: app.name })}</div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground" aria-label={t("settings.marketplace.installModal.close")}>
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-4">
          <div className="text-sm text-muted-foreground leading-relaxed">
            {t("settings.marketplace.uninstallModal.removesBefore")}<span className="font-mono text-xs">{app.id}</span>{t("settings.marketplace.uninstallModal.removesMiddle")}
            <strong className="text-foreground">{t("settings.marketplace.uninstallModal.wipesDb")}</strong>{t("settings.marketplace.uninstallModal.removesAt")}
            <span className="font-mono text-xs"> data/apps/{app.id}/</span>.
          </div>
          <div className="text-xs text-muted-foreground">
            {t("settings.marketplace.uninstallModal.reinstallNote")}
          </div>
          <div className="flex gap-2 pt-2">
            <button
              onClick={onClose}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium border border-border hover:bg-muted transition"
            >
              {t("settings.marketplace.uninstallModal.keep")}
            </button>
            <button
              onClick={onConfirm}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium bg-destructive text-white hover:opacity-90 transition"
            >
              {t("settings.marketplace.uninstallModal.confirm")}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}


function WhatsAppOptions({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
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
        ? t("settings.whatsappOptions.statusOn")
        : t("settings.whatsappOptions.statusOff"), "success");
    } catch {
      toast(t("settings.whatsappOptions.updateFailed"), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border-t border-border bg-muted/20 px-5 py-3 flex items-start gap-4">
      <div className="flex-1 text-sm">
        <div className="font-medium">{t("settings.whatsappOptions.title")}</div>
        <div className="text-xs text-muted-foreground mt-0.5 leading-relaxed">
          {enabled
            ? t("settings.whatsappOptions.onNote")
            : t("settings.whatsappOptions.offNote")}
          {" "}{t("settings.whatsappOptions.alwaysNote")}
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




function MapsConnectorCard() {
  const { t } = useTranslation();
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
      setStatus(t("settings.maps.enterKey"));
      return;
    }
    setBusy(true);
    setStatus(null);
    try {
      await api.post("/api/connectors/maps/credentials", { credentials: { ors_api_key: key } });
      setStatus("✓ " + t("settings.maps.saved"));
      setConfigured(true);
      setApiKey("");
    } catch (err: any) {
      setStatus(t("settings.maps.error", { msg: err?.message || err }));
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    if (!confirm(t("settings.maps.removeConfirm"))) return;
    setBusy(true);
    setStatus(null);
    try {
      await api.delete("/api/connectors/maps/credentials");
      setStatus("✓ " + t("settings.maps.removed"));
      setConfigured(false);
    } catch (err: any) {
      setStatus(t("settings.maps.error", { msg: err?.message || err }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <div className="flex items-start gap-3 mb-3">
        <div className="flex-1 min-w-0">
          <div className="font-semibold">{t("settings.maps.title")}</div>
          <div className="text-sm text-muted-foreground mt-1 leading-relaxed">
            {t("settings.maps.introBefore")}<em>{t("settings.maps.introEm")}</em>{t("settings.maps.introAfter")}
          </div>
        </div>
        {configured !== null && (
          <span className={cn(
            "text-xs px-2 py-0.5 rounded shrink-0",
            configured
              ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
              : "bg-muted text-muted-foreground"
          )}>
            {configured ? t("settings.maps.orsActive") : t("settings.maps.osrmDemo")}
          </span>
        )}
      </div>

      <Field label={t("settings.maps.keyLabel")}>
        <input
          type="password"
          value={apiKey}
          onChange={e => setApiKey(e.target.value)}
          placeholder={configured ? t("settings.maps.keyStoredPlaceholder") : t("settings.maps.keyPlaceholder")}
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
          {busy ? t("settings.maps.saving") : t("settings.maps.save")}
        </button>
        {configured && (
          <button
            onClick={clear}
            disabled={busy}
            className="text-sm px-3 py-1.5 rounded-md border border-border hover:bg-muted disabled:opacity-50 transition"
          >
            {t("settings.maps.remove")}
          </button>
        )}
        <a
          href="https://openrouteservice.org/dev/#/signup"
          target="_blank" rel="noopener"
          className="text-xs text-muted-foreground hover:text-foreground underline ml-auto"
        >
          {t("settings.maps.getKey")}
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
  const { t } = useTranslation();
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
      else toast(t("settings.extensions.loadFailed", { msg: e.message }), "error");
    } finally {
      setLoading(false);
    }
  }, [toast, t]);

  useEffect(() => { refresh(); }, [refresh]);

  async function install(extId: string) {
    setInstalling(extId);
    try {
      const r = await api.post<{ ok: boolean; returncode?: number; stdout?: string; stderr?: string; error?: string }>(
        `/api/extensions/${extId}/install`, {},
      );
      if (r.ok) {
        toast(t("settings.extensions.installed", { id: extId }), "success");
      } else {
        toast(t("settings.extensions.installFailedMsg", { msg: r.error || `pip exit ${r.returncode}` }), "error");
      }
      await refresh();
    } catch (e: any) {
      toast(e?.message || t("settings.extensions.installFailed"), "error");
    } finally {
      setInstalling(null);
    }
  }

  if (forbidden) {
    return (
      <div className="text-sm text-muted-foreground py-8 text-center">
        {t("settings.extensions.adminOnly")}
      </div>
    );
  }

  return (
    <div>
      <header className="mb-6">
        <h2 className="text-lg font-semibold">{t("settings.extensions.title")}</h2>
        <p className="text-sm text-muted-foreground mt-1">
          {t("settings.extensions.intro")}
        </p>
      </header>

      {loading && !rows && (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
        </div>
      )}

      {rows && rows.length === 0 && (
        <div className="text-sm text-muted-foreground py-8 text-center">
          {t("settings.extensions.noneBefore")}<code>extensions/</code>.
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
                    <span className="text-2xs text-muted-foreground font-mono">v{r.version}</span>
                    {r.country && (
                      <span className="text-2xs px-1.5 py-0.5 rounded bg-muted/60 text-muted-foreground">{r.country}</span>
                    )}
                    {r.loaded ? (
                      <span className="text-2xs inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 border border-emerald-500/20">
                        <CheckCircle2 className="w-2.5 h-2.5" /> {t("settings.extensions.active")}
                      </span>
                    ) : r.deps.all_met ? (
                      <span className="text-2xs inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-600 border border-amber-500/20">
                        <AlertCircle className="w-2.5 h-2.5" /> {t("settings.extensions.restartPending")}
                      </span>
                    ) : (
                      <span className="text-2xs inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground border border-border">
                        <X className="w-2.5 h-2.5" /> {t("settings.extensions.depsMissing")}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1.5">{r.description}</p>
                  {r.python_requirements.length > 0 && (
                    <div className="mt-2 text-2xs text-muted-foreground">
                      <span className="font-medium">{t("settings.extensions.pythonDeps")}</span>{" "}
                      <span className="font-mono">{r.python_requirements.join(", ")}</span>
                    </div>
                  )}
                  {r.errors.length > 0 && (
                    <div className="mt-2 text-2xs text-rose-600">
                      <span className="font-medium">{t("settings.extensions.errors")}</span> {r.errors.join(" / ")}
                    </div>
                  )}
                  {r.docs_url && (
                    <a
                      href={r.docs_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-2xs text-blue-500 hover:underline inline-flex items-center gap-1 mt-2"
                    >
                      <ExternalLink className="w-2.5 h-2.5" /> {t("settings.extensions.docs")}
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
                      {installing === r.id ? t("settings.extensions.installing") : t("settings.extensions.install")}
                    </button>
                  )}
                  {r.deps.all_met && !r.loaded && (
                    <button
                      type="button"
                      onClick={refresh}
                      className="px-3 py-1.5 text-xs rounded-md font-medium bg-muted hover:bg-muted/70 inline-flex items-center gap-1.5"
                    >
                      <RefreshCw className="w-3.5 h-3.5" /> {t("settings.extensions.recheck")}
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




/** error_log stores UTC without a zone; show it in local time. */
function fmtLogTime(ts: string): string {
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts : ts.replace(" ", "T") + "Z");
  return isNaN(d.getTime()) ? ts.replace("T", " ") : d.toLocaleString(locale());
}

function LogsTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
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
        toast(t("settings.logs.loadFailed", { msg }), "error");
      }
    } finally {
      setLoading(false);
    }
  }, [level, limit, toast, t]);

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
      () => toast(t("settings.logs.copied", { id: corr }), "success"),
      () => toast(t("settings.logs.copyFailed"), "error"),
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
          <h1 className="text-2xl font-semibold">{t("settings.logs.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {t("settings.logs.introBefore")}<code className="text-xs">journalctl -u yorik</code>{t("settings.logs.introAfter")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={level}
            onChange={e => setLevel(e.target.value as typeof level)}
            className={cn(inputClass, "h-8 text-xs w-auto")}
          >
            <option value="">{t("settings.logs.allLevels")}</option>
            <option value="WARNING">{t("settings.logs.levels.warning")}</option>
            <option value="ERROR">{t("settings.logs.levels.error")}</option>
            <option value="CRITICAL">{t("settings.logs.levels.critical")}</option>
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
            title={t("settings.logs.reload")}
          >
            <RefreshCw className={cn("w-3.5 h-3.5", loading && "animate-spin")} />
          </button>
        </div>
      </header>

      {forbidden && (
        <Card title={t("settings.logs.adminOnly")}>
          <div className="flex items-start gap-3 py-2 text-sm">
            <Shield className="w-4 h-4 text-muted-foreground mt-0.5 shrink-0" />
            <div className="text-muted-foreground">
              {t("settings.logs.adminOnlyNote")}
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
          <Card title={t("settings.logs.byLevel")}>
            {Object.keys(data.summary).length === 0 ? (
              <EmptyMetric icon={Info} label={t("settings.logs.noneRecorded")} />
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

          <Card title={t(`settings.logs.recent.${level || "all"}`, { count: data.errors.length })}>
            {data.errors.length === 0 ? (
              <EmptyMetric icon={ScrollText} label={t("settings.logs.noneMatching")} />
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
                            "inline-flex items-center px-1.5 py-0.5 rounded text-2xs font-semibold border shrink-0 mt-0.5",
                            levelClasses(row.level),
                          )}
                        >
                          {row.level}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-baseline gap-2 flex-wrap">
                            <span className="font-mono text-xs text-muted-foreground tabular-nums shrink-0">
                              {fmtLogTime(row.ts)}
                            </span>
                            <span className="font-mono text-xs text-muted-foreground truncate">
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
                              <span className="text-muted-foreground shrink-0">{t("settings.logs.path")}</span>
                              <code className="font-mono break-all">{row.request_path}</code>
                            </div>
                          )}
                          {row.corr_id && (
                            <div className="flex items-center gap-2">
                              <span className="text-muted-foreground shrink-0">{t("settings.logs.corr")}</span>
                              <code className="font-mono">{row.corr_id}</code>
                              <button
                                onClick={() => copyCorr(row.corr_id!)}
                                title={t("settings.logs.copyCorr")}
                                className="text-muted-foreground hover:text-foreground"
                              >
                                <Copy className="w-3 h-3" />
                              </button>
                              <span className="text-muted-foreground/70 text-2xs">
                                {" — "}
                                <code className="font-mono">journalctl -u yorik | grep {row.corr_id}</code>
                              </span>
                            </div>
                          )}
                          {row.traceback && (
                            <pre className="bg-muted/40 border border-border rounded-md p-2 overflow-x-auto whitespace-pre text-xs leading-snug">
                              {row.traceback}
                            </pre>
                          )}
                          {!row.traceback && !row.corr_id && !row.request_path && (
                            <div className="text-muted-foreground italic">
                              {t("settings.logs.noContext")}
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
      <h3 className="text-xs font-semibold text-muted-foreground mb-3">{title}</h3>
      {children}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
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
  user_id: string;   // UUID
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
  owner_user_id: string | null;
  members_count: number;
  your_level: "read" | "write" | "admin" | null;
}
interface SpaceDetail extends Space { members: SpaceMember[] }
interface UserRow { id: string; name: string; email: string; role: string }
interface Workspace { id: number; name: string; kind: "family" | "business"; owner_user_id: string }

function SpacesTab({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
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
      toast(t("settings.spaces.loadFailed", { msg: e?.message || e }), "error");
    } finally {
      setLoading(false);
    }
  }, [selectedSpaceId, toast, t]);

  useEffect(() => { void refreshSpaces(); }, [refreshSpaces]);

  useEffect(() => {
    if (selectedSpaceId === null) { setDetail(null); return; }
    (async () => {
      try {
        const d = await api.get<SpaceDetail>(`/api/spaces/${selectedSpaceId}`);
        setDetail(d);
      } catch (e: any) {
        toast(t("settings.spaces.loadOneFailed", { msg: e?.message || e }), "error");
        setDetail(null);
      }
    })();
  }, [selectedSpaceId, toast, t]);

  // Lazy-load users only when admin opens the add-member picker.
  async function ensureUsers() {
    if (allUsers.length > 0) return;
    try { setAllUsers(await api.get<UserRow[]>("/api/users")); }
    catch (e: any) { toast(t("settings.spaces.listUsersFailed", { msg: e?.message || e }), "error"); }
  }

  async function setWorkspaceKind(kind: "family" | "business") {
    if (!isAdmin || !workspace) return;
    try {
      const next = await api.patch<Workspace>("/api/workspaces/current", { kind });
      setWorkspace(next);
      toast(t("settings.spaces.kindSet", { kind: t(`settings.spaces.kinds.${kind}`) }), "success");
    } catch (e: any) {
      toast(t("settings.spaces.updateWorkspaceFailed", { msg: e?.message || e }), "error");
    }
  }

  async function createSpace(name: string, slug: string) {
    if (!isAdmin) return;
    try {
      const d = await api.post<SpaceDetail>("/api/spaces", { name, slug: slug || undefined });
      await refreshSpaces();
      setSelectedSpaceId(d.id);
      setCreating(false);
      toast(t("settings.spaces.created", { name: d.name }), "success");
    } catch (e: any) {
      toast(t("settings.spaces.createFailed", { msg: e?.message || e }), "error");
    }
  }

  async function deleteSpace(s: Space) {
    if (!isAdmin) return;
    if (!confirm(t("settings.spaces.deleteConfirm", { name: s.name }))) return;
    try {
      await api.delete(`/api/spaces/${s.id}`);
      setSelectedSpaceId(null);
      await refreshSpaces();
      toast(t("settings.spaces.deleted", { name: s.name }), "success");
    } catch (e: any) {
      toast(t("settings.spaces.deleteFailed", { msg: e?.message || e }), "error");
    }
  }

  async function addMember(user_id: string, level: "read" | "write" | "admin") {
    if (!isAdmin || !detail) return;
    try {
      const d = await api.post<SpaceDetail>(`/api/spaces/${detail.id}/members`,
        { user_id, level });
      setDetail(d);
      await refreshSpaces();
      toast(t("settings.spaces.memberAdded"), "success");
    } catch (e: any) {
      toast(t("settings.spaces.addMemberFailed", { msg: e?.message || e }), "error");
    }
  }

  async function patchMemberLevel(user_id: string, level: "read" | "write" | "admin") {
    if (!isAdmin || !detail) return;
    try {
      const d = await api.patch<SpaceDetail>(
        `/api/spaces/${detail.id}/members/${user_id}`, { level });
      setDetail(d);
    } catch (e: any) {
      toast(t("settings.spaces.updateLevelFailed", { msg: e?.message || e }), "error");
    }
  }

  async function removeMember(user_id: string) {
    if (!isAdmin || !detail) return;
    try {
      await api.delete(`/api/spaces/${detail.id}/members/${user_id}`);
      const d = await api.get<SpaceDetail>(`/api/spaces/${detail.id}`);
      setDetail(d);
      await refreshSpaces();
    } catch (e: any) {
      toast(t("settings.spaces.removeFailed", { msg: e?.message || e }), "error");
    }
  }

  if (loading && !workspace) {
    return <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="w-4 h-4 animate-spin" /> {t("settings.spaces.loading")}</div>;
  }

  return (
    <div className="space-y-6">
      {/* Workspace kind */}
      {workspace && (
        <div className="rounded-2xl border border-border bg-card p-5">
          <div className="text-xs text-muted-foreground font-semibold mb-2">{t("settings.spaces.household")}</div>
          <div className="flex items-center gap-3">
            <div className="flex-1 min-w-0">
              <div className="font-medium text-base truncate">{workspace.name}</div>
              <div className="text-xs text-muted-foreground mt-0.5">
                {t("settings.spaces.kindNote")}
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
                  title={!isAdmin ? t("settings.spaces.adminOnly") : ""}
                >{t(`settings.spaces.kinds.${k}`)}</button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Spaces list + detail */}
      <div className="grid md:grid-cols-[280px_1fr] gap-4">
        <div className="space-y-1">
          <div className="flex items-center justify-between mb-2">
            <div className="text-xs text-muted-foreground font-semibold">{t("settings.spaces.title")}</div>
            {isAdmin && (
              <button
                onClick={() => setCreating(true)}
                className="text-xs text-blue-600 dark:text-blue-300 hover:underline"
              >{t("settings.spaces.new")}</button>
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
                <div className="text-2xs text-muted-foreground">
                  {s.kind === "shared" ? t("settings.spaces.members", { count: s.members_count }) : t("settings.spaces.personal")}
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
            <div className="text-sm text-muted-foreground italic">{t("settings.spaces.pickOne")}</div>
          )}
        </div>
      </div>
    </div>
  );
}

function CreateSpaceForm({ onCancel, onCreate }: { onCancel: () => void; onCreate: (n: string, s: string) => void }) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  return (
    <div className="rounded-2xl border border-border bg-card p-5">
      <div className="text-xs text-muted-foreground font-semibold mb-3">{t("settings.spaces.create.title")}</div>
      <div className="space-y-3">
        <div>
          <label className="text-xs text-muted-foreground">{t("settings.spaces.create.name")}</label>
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder={t("settings.spaces.create.namePlaceholder")}
            className="mt-1 w-full px-3 py-2 rounded-lg border border-border bg-background text-sm"
            autoFocus
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground">{t("settings.spaces.create.slug")}</label>
          <input
            value={slug}
            onChange={e => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))}
            placeholder={t("settings.spaces.create.slugPlaceholder")}
            className="mt-1 w-full px-3 py-2 rounded-lg border border-border bg-background text-sm font-mono"
          />
          <div className="text-2xs text-muted-foreground mt-1">
            {t("settings.spaces.create.slugNote")}
          </div>
        </div>
        <div className="flex gap-2 pt-1">
          <button
            onClick={() => onCreate(name.trim(), slug.trim())}
            disabled={!name.trim()}
            className="px-4 py-1.5 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
          >{t("settings.spaces.create.create")}</button>
          <button
            onClick={onCancel}
            className="px-4 py-1.5 rounded-md border border-border text-sm hover:bg-muted"
          >{t("settings.spaces.create.cancel")}</button>
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
  onAddMember: (uid: string, level: "read" | "write" | "admin") => void;
  onPatchLevel: (uid: string, level: "read" | "write" | "admin") => void;
  onRemoveMember: (uid: string) => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const [adding, setAdding] = useState(false);
  const [pickUserId, setPickUserId] = useState<string>("");
  const [pickLevel, setPickLevel] = useState<"read" | "write" | "admin">("write");

  const isPersonal = detail.kind === "personal";
  const memberIds = new Set(detail.members.map(m => m.user_id));
  const availableUsers = allUsers.filter(u => !memberIds.has(u.id));

  return (
    <div className="rounded-2xl border border-border bg-card p-5 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-base font-semibold">{detail.name}</div>
          <div className="text-xs text-muted-foreground mt-0.5">
            {isPersonal ? t("settings.spaces.detail.personalIntro") :
              `${t("settings.spaces.detail.sharedSpace")}${detail.slug ? ` · ${t("settings.spaces.detail.slug", { slug: detail.slug })}` : ""} · ${t("settings.spaces.members", { count: detail.members.length })}`}
          </div>
        </div>
        {isAdmin && !isPersonal && detail.slug !== "household" && detail.slug !== "finance" && (
          <button
            onClick={onDelete}
            className="text-xs text-rose-600 dark:text-rose-400 hover:underline shrink-0"
          >{t("settings.spaces.detail.delete")}</button>
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
              <div className="text-xs text-muted-foreground truncate">
                {m.email}
                {(m.paperless_user_id || m.immich_user_id) && (
                  <span className="ml-2 text-2xs">
                    {m.paperless_user_id ? "· paperless ✓" : ""}{m.immich_user_id ? " · immich ✓" : ""}
                  </span>
                )}
                {!m.paperless_user_id && !m.immich_user_id && (
                  <span className="ml-2 text-2xs text-amber-600 dark:text-amber-400">
                    · {t("settings.spaces.detail.noExternalAccounts")}
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
                  <option value="read">{t("settings.spaces.detail.levels.read")}</option>
                  <option value="write">{t("settings.spaces.detail.levels.write")}</option>
                  <option value="admin">{t("settings.spaces.detail.levels.admin")}</option>
                </select>
                <button
                  onClick={() => onRemoveMember(m.user_id)}
                  className="text-rose-600 dark:text-rose-400 hover:bg-rose-500/10 p-1.5 rounded"
                  title={t("settings.spaces.detail.removeMember")}
                >
                  <X className="w-4 h-4" />
                </button>
              </>
            ) : (
              <span className="text-xs text-muted-foreground">
                {t(`settings.spaces.detail.levelShort.${m.level}`)}
              </span>
            )}
          </div>
        ))}
        {detail.members.length === 0 && (
          <div className="text-sm text-muted-foreground italic px-3 py-2">{t("settings.spaces.detail.noMembers")}</div>
        )}
      </div>

      {isAdmin && !isPersonal && (
        <div className="pt-2 border-t border-border">
          {!adding ? (
            <button
              onClick={async () => { await ensureUsers(); setAdding(true); }}
              className="text-sm text-blue-600 dark:text-blue-300 hover:underline inline-flex items-center gap-1"
            >
              <UserPlus className="w-4 h-4" /> {t("settings.spaces.detail.addMember")}
            </button>
          ) : (
            <div className="flex items-center gap-2">
              <select
                value={pickUserId}
                onChange={e => setPickUserId(e.target.value)}
                className="flex-1 px-2 py-1.5 rounded border border-border bg-background text-sm"
              >
                <option value="">{t("settings.spaces.detail.pickUser")}</option>
                {availableUsers.map(u => (
                  <option key={u.id} value={u.id}>{u.name} ({u.email})</option>
                ))}
              </select>
              <select
                value={pickLevel}
                onChange={e => setPickLevel(e.target.value as any)}
                className="px-2 py-1.5 rounded border border-border bg-background text-sm"
              >
                <option value="read">{t("settings.spaces.detail.levels.read")}</option>
                <option value="write">{t("settings.spaces.detail.levels.write")}</option>
                <option value="admin">{t("settings.spaces.detail.levels.admin")}</option>
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
              >{t("settings.spaces.detail.add")}</button>
              <button
                onClick={() => { setAdding(false); setPickUserId(""); }}
                className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted"
              >{t("settings.spaces.detail.cancel")}</button>
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
  const { t } = useTranslation();
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
      if (e?.status && e.status !== 403) toast(t("settings.installedApps.loadFailed"), "error");
    }
  }, [toast, t]);

  useEffect(() => { void load(); }, [load]);

  async function doUninstall(app: InstalledV2App) {
    setPendingUninstall(null);
    setBusy(app.app_id);
    try {
      await api.delete(`/api/apps/${app.app_id}?wipe_data=true`);
      toast(t("settings.installedApps.uninstalled", { name: app.manifest?.name || app.app_id }), "success");
      await load();
    } catch {
      toast(t("settings.installedApps.uninstallFailed", { name: app.app_id }), "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">{t("settings.installedApps.title")}</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t("settings.installedApps.intro")}
        </p>
      </header>

      {/* Install-from-path admin form */}
      <div className="mb-5 rounded-xl border border-border bg-card p-4">
        <div className="text-2xs font-semibold text-muted-foreground mb-2">
          {t("settings.installedApps.installFromDir")}
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
            {t("settings.installedApps.previewInstall")}
          </button>
        </div>
        <p className="text-xs text-muted-foreground mt-2 leading-relaxed">
          {t("settings.installedApps.installFromDirNote")}
        </p>
      </div>

      {apps === null && (
        <div className="text-sm text-muted-foreground">{t("settings.installedApps.loading")}</div>
      )}

      {apps && apps.length === 0 && (
        <div className="bg-card border border-border rounded-xl p-8 text-sm text-muted-foreground text-center">
          <Package className="w-6 h-6 mx-auto mb-2 opacity-50" />
          <div>{t("settings.installedApps.emptyTitle")}</div>
          <div className="mt-1 text-xs">
            {t("settings.installedApps.emptyHint")}
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
            toast(t("settings.installedApps.installed", { name: appId }), "success");
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
  const { t } = useTranslation();
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
          <div className="font-semibold">{t("settings.installedApps.uninstallModal.question", { name })}</div>
          <button onClick={onClose} className="p-1.5 hover:bg-muted rounded-md text-muted-foreground" aria-label={t("settings.installedApps.uninstallModal.close")}>
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-4">
          <div className="text-sm text-muted-foreground leading-relaxed">
            {t("settings.installedApps.uninstallModal.removesBefore")}<span className="font-mono text-xs">{app.app_id}</span>{t("settings.installedApps.uninstallModal.removesMiddle")}
            <strong className="text-foreground">{t("settings.installedApps.uninstallModal.wipesSchema")}</strong>
            {" "}<span className="font-mono text-xs">{app.owned_schema}</span>
            {t("settings.installedApps.uninstallModal.plusDataDir")}
            <span className="font-mono text-xs"> data/apps/{app.app_id}/</span>.
          </div>
          <div className="text-xs text-muted-foreground">
            {t("settings.installedApps.uninstallModal.reinstallNote")}
          </div>
          <div className="flex gap-2 pt-2">
            <button
              onClick={onClose}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium border border-border hover:bg-muted transition"
            >
              {t("settings.installedApps.uninstallModal.keep")}
            </button>
            <button
              onClick={onConfirm}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium bg-destructive text-white hover:opacity-90 transition"
            >
              {t("settings.installedApps.uninstallModal.confirm")}
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
  const { t } = useTranslation();
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
            <span className="text-2xs font-mono text-muted-foreground">
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
          {busy ? t("settings.installedApps.card.uninstalling") : t("settings.installedApps.card.uninstall")}
        </button>
      </div>

      <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
        <PermFact
          label={t("settings.installedApps.card.ownedSchema")}
          value={
            <span className="font-mono text-xs">{app.owned_schema}</span>
          }
        />
        <PermFact
          label={t("settings.installedApps.card.tables")}
          value={
            ownedTables.length > 0 ? (
              <span className="font-mono text-xs">{ownedTables.join(", ")}</span>
            ) : <span className="text-muted-foreground">—</span>
          }
        />
        <PermFact
          label={t("settings.installedApps.card.reads", { count: reads.length })}
          value={
            reads.length > 0 ? (
              <span className="text-foreground">
                {reads.map((r: any) => r.table).join(", ")}
              </span>
            ) : <span className="text-muted-foreground">{t("settings.installedApps.card.none")}</span>
          }
        />
        <PermFact
          label={t("settings.installedApps.card.skills", { count: skills.length })}
          value={
            skills.length > 0 ? (
              <span className="text-foreground">{skills.join(", ")}</span>
            ) : <span className="text-muted-foreground">{t("settings.installedApps.card.none")}</span>
          }
        />
        {realtime.length > 0 && (
          <PermFact
            label={t("settings.installedApps.card.realtime")}
            value={
              <span className="font-mono text-xs">{realtime.join(", ")}</span>
            }
          />
        )}
        {app.source_dir && (
          <PermFact
            label={t("settings.installedApps.card.source")}
            value={
              <span className="font-mono text-2xs text-muted-foreground break-all">
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
      <div className="text-2xs font-semibold text-muted-foreground mb-0.5">
        {label}
      </div>
      <div>{value}</div>
    </div>
  );
}
