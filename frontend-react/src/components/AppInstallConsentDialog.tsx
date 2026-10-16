/**
 * Phase E §7 consent dialog for manifest v2 community apps.
 *
 * Driven entirely by the structured payload returned by
 * POST /api/apps/install/preflight — what the user sees on screen
 * is exactly what the backend logs as "this is what the admin
 * agreed to" when /api/apps/install/confirm runs.
 *
 * Two display rules from the masterplan:
 *   1. Every grant in plain language with the manifest's "why".
 *   2. The negative space is shown ("this app CANNOT…") so an admin
 *      seeing nothing on a line knows that's deliberate, not missing.
 *
 * Input: either { manifest } (preview a manifest you have in hand)
 * or { sourceDir } (let the backend read manifest.json itself).
 *
 * On confirm: requires sourceDir. The "preview-only" path is for
 * dev tooling that wants to render the dialog without persisting.
 */

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import {
  X, CheckCircle2, AlertTriangle, Lock, Globe, Download,
  ShieldCheck, Loader2,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

interface PreflightSummary {
  app: {
    id: string | null;
    name: string | null;
    version: string | null;
    author: string | null;
    license: string | null;
    homepage: string | null;
    description: string | null;
  };
  owned_schema: string | null;
  owned_tables: string[];
  scopes: Array<
    | { kind: "read"; table: string; columns: string[]; purpose: string }
    | { kind: "write_rejected"; table: string }
    | { kind: "skill"; skill: string }
    | { kind: "connector"; connector: string }
    | { kind: "realtime"; table: string }
    | { kind: "scheduled"; cron: string; invokes: string; purpose: string }
    | { kind: "webhook"; path: string; purpose: string }
  >;
  network: {
    talks_only_to_yorik: boolean;
    outbound: Array<{ url: string; purpose: string }>;
  };
  cannot: string[];
  manifest_version: number;
}

interface PreflightResponse {
  manifest: Record<string, unknown>;
  errors: string[];
  summary: PreflightSummary;
  iframe_csp: string;
}

interface Props {
  /** Either supply a manifest object (preview-only) or a sourceDir (installable). */
  manifest?: Record<string, unknown>;
  sourceDir?: string;
  onClose: () => void;
  onInstalled?: (appId: string) => void;
}

export function AppInstallConsentDialog({
  manifest, sourceDir, onClose, onInstalled,
}: Props) {
  const { t } = useTranslation();
  const [data, setData] = useState<PreflightResponse | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [installing, setInstalling] = useState(false);
  const [installErr, setInstallErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = manifest ? { manifest } : { source_dir: sourceDir };
        const resp = await api.post<PreflightResponse>(
          "/api/apps/install/preflight",
          body,
        );
        if (!cancelled) setData(resp);
      } catch (e) {
        if (cancelled) return;
        setLoadErr(
          e instanceof ApiError
            ? e.message || t("settings.appInstall.preflightFailedHttp", { status: e.status })
            : t("settings.appInstall.preflightFailed"),
        );
      }
    })();
    return () => { cancelled = true; };
  }, [manifest, sourceDir, t]);

  const handleInstall = useCallback(async () => {
    if (!sourceDir) {
      setInstallErr(t("settings.appInstall.noSourceDir"));
      return;
    }
    setInstalling(true);
    setInstallErr(null);
    try {
      const resp = await api.post<{ app_id: string }>(
        "/api/apps/install/confirm",
        { source_dir: sourceDir, shown_summary: data?.summary ?? null },
      );
      onInstalled?.(resp.app_id);
      onClose();
    } catch (e) {
      setInstallErr(
        e instanceof ApiError
          ? e.message || t("settings.appInstall.installFailedHttp", { status: e.status })
          : t("settings.appInstall.installFailed"),
      );
      setInstalling(false);
    }
  }, [sourceDir, data?.summary, onInstalled, onClose, t]);

  return createPortal(
    <div
      className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg bg-card border border-border rounded-2xl shadow-2xl overflow-hidden max-h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="font-semibold">
            {data ? t("settings.appInstall.installQuestion", { name: data.summary.app.name ?? t("settings.appInstall.app") }) : t("settings.appInstall.loading")}
          </div>
          <button
            onClick={onClose}
            className="p-1.5 hover:bg-muted rounded-md text-muted-foreground"
            aria-label={t("settings.appInstall.close")}
          >
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="p-5 space-y-4 overflow-y-auto">
          {loadErr && <ErrorBlock message={loadErr} />}

          {!data && !loadErr && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground py-8 justify-center">
              <Loader2 className="w-4 h-4 animate-spin" />
              {t("settings.appInstall.readingManifest")}
            </div>
          )}

          {data && data.errors.length > 0 && (
            <ManifestErrors errors={data.errors} />
          )}

          {data && data.errors.length === 0 && (
            <ConsentBody data={data} />
          )}

          {installErr && <ErrorBlock message={installErr} />}
        </div>

        <footer className="px-5 py-4 border-t border-border flex gap-2">
          <button
            onClick={onClose}
            disabled={installing}
            className={cn(
              "flex-1 px-3 py-2 rounded-md text-sm font-medium border border-border",
              "hover:bg-muted text-muted-foreground hover:text-foreground transition",
              installing && "opacity-50 cursor-wait",
            )}
          >
            {t("settings.appInstall.cancel")}
          </button>
          <button
            onClick={handleInstall}
            disabled={
              installing || !data || data.errors.length > 0 || !sourceDir
            }
            className={cn(
              "flex-1 px-3 py-2 rounded-md text-sm font-medium bg-primary text-primary-foreground",
              "hover:bg-primary/90 transition inline-flex items-center justify-center gap-1.5",
              (installing || !data || data.errors.length > 0 || !sourceDir) &&
                "opacity-50 cursor-not-allowed",
            )}
          >
            {installing ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                {t("settings.appInstall.installing")}
              </>
            ) : (
              <>
                <Download className="w-3.5 h-3.5" />
                {t("settings.appInstall.install")}
              </>
            )}
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  );
}

function ConsentBody({ data }: { data: PreflightResponse }) {
  const s = data.summary;
  return (
    <>
      <AppHeader app={s.app} />
      {s.owned_tables.length > 0 && (
        <OwnedDataBlock schema={s.owned_schema} tables={s.owned_tables} />
      )}
      {s.scopes.length > 0 && <ScopesBlock scopes={s.scopes} />}
      <NetworkBlock network={s.network} />
      {s.cannot.length > 0 && <CannotBlock cannot={s.cannot} />}
      <CspBlock csp={data.iframe_csp} />
    </>
  );
}

function AppHeader({ app }: { app: PreflightSummary["app"] }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-start gap-3">
      <div className="min-w-0">
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="font-semibold">{app.name}</span>
          {app.version && (
            <span className="text-xs text-muted-foreground">v{app.version}</span>
          )}
        </div>
        {(app.author || app.license || app.homepage) && (
          <div className="text-xs text-muted-foreground mt-0.5">
            {app.author && <>{t("settings.appInstall.by", { author: app.author })}</>}
            {app.author && app.license && <> · </>}
            {app.license && <>{app.license}</>}
            {app.homepage && (
              <>
                {" · "}
                <a
                  href={app.homepage}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline hover:text-foreground"
                >
                  {t("settings.appInstall.source")}
                </a>
              </>
            )}
          </div>
        )}
        {app.description && (
          <p className="text-sm text-muted-foreground mt-2 leading-relaxed">
            {app.description}
          </p>
        )}
      </div>
    </div>
  );
}

function OwnedDataBlock({
  schema, tables,
}: { schema: string | null; tables: string[] }) {
  const { t } = useTranslation();
  return (
    <Section title={t("settings.appInstall.ownData")}>
      <div className="text-[13px] flex items-start gap-2">
        <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
        <span>
          {t("settings.appInstall.privateTables")}{" "}
          <span className="font-mono text-xs text-muted-foreground">
            {schema}
          </span>
          : {tables.map((t, i) => (
            <span key={t}>
              <span className="font-mono text-xs text-muted-foreground">
                {t}
              </span>
              {i < tables.length - 1 && ", "}
            </span>
          ))}
        </span>
      </div>
    </Section>
  );
}

function ScopesBlock({ scopes }: { scopes: PreflightSummary["scopes"] }) {
  const { t } = useTranslation();
  return (
    <Section title={t("settings.appInstall.wantsTo")}>
      <ul className="space-y-1.5 text-[13px]">
        {scopes.map((s, i) => (
          <li key={i} className="flex items-start gap-2">
            {s.kind === "write_rejected" ? (
              <AlertTriangle className="w-3.5 h-3.5 text-red-500 shrink-0 mt-0.5" />
            ) : (
              <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
            )}
            <span><ScopeLine scope={s} /></span>
          </li>
        ))}
      </ul>
    </Section>
  );
}

function ScopeLine({ scope }: { scope: PreflightSummary["scopes"][number] }) {
  const { t } = useTranslation();
  if (scope.kind === "read") {
    return (
      <>
        <strong>{t("settings.appInstall.read")}</strong> {t("settings.appInstall.your")}{" "}
        <span className="font-mono text-xs">{scope.table}</span>
        {scope.columns.length > 0 && (
          <>
            {" "}(<span className="text-muted-foreground">
              {scope.columns.join(", ")}
            </span>)
          </>
        )}
        {scope.purpose && (
          <div className="text-xs text-muted-foreground mt-0.5">
            {t("settings.appInstall.why")} {scope.purpose}
          </div>
        )}
      </>
    );
  }
  if (scope.kind === "write_rejected") {
    return (
      <>
        <strong className="text-red-600 dark:text-red-400">{t("settings.appInstall.write")}</strong> {t("settings.appInstall.to")}{" "}
        <span className="font-mono text-xs">{scope.table}</span>{" "}
        <span className="text-xs text-muted-foreground">
          {t("settings.appInstall.writeRejected")}
        </span>
      </>
    );
  }
  if (scope.kind === "skill") {
    return (
      <>
        {t("settings.appInstall.runSkill")}{" "}
        <span className="font-mono text-xs">{scope.skill}</span>
      </>
    );
  }
  if (scope.kind === "connector") {
    return (
      <>
        {t("settings.appInstall.useThe")}{" "}
        <span className="font-mono text-xs">{scope.connector}</span>{" "}
        {t("settings.appInstall.connector")}
      </>
    );
  }
  if (scope.kind === "realtime") {
    return (
      <>
        {t("settings.appInstall.realtime")}{" "}
        <span className="font-mono text-xs">{scope.table}</span>
      </>
    );
  }
  if (scope.kind === "scheduled") {
    return (
      <>
        {t("settings.appInstall.run")} <span className="font-mono text-xs">{scope.invokes}</span>{" "}
        {t("settings.appInstall.onSchedule")}{" "}
        <span className="text-muted-foreground">({scope.cron})</span>
        {scope.purpose && (
          <div className="text-xs text-muted-foreground mt-0.5">
            {t("settings.appInstall.why")} {scope.purpose}
          </div>
        )}
      </>
    );
  }
  // webhook
  return (
    <>
      {t("settings.appInstall.webhook")}{" "}
      <span className="font-mono text-xs">{scope.path}</span>
      {scope.purpose && (
        <div className="text-xs text-muted-foreground mt-0.5">
          {t("settings.appInstall.why")} {scope.purpose}
        </div>
      )}
    </>
  );
}

function NetworkBlock({ network }: { network: PreflightSummary["network"] }) {
  const { t } = useTranslation();
  if (network.talks_only_to_yorik) {
    return (
      <Section title={t("settings.appInstall.network")}>
        <div className="flex items-start gap-2 text-[13px]">
          <ShieldCheck className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
          <span>{t("settings.appInstall.onlyYorik")}</span>
        </div>
      </Section>
    );
  }
  return (
    <Section title={t("settings.appInstall.network")}>
      <div className="text-[13px] mb-1.5 flex items-start gap-2">
        <Globe className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
        <span>{t("settings.appInstall.mayContact")}</span>
      </div>
      <ul className="space-y-1 text-[13px] ml-5">
        {network.outbound.map((o, i) => (
          <li key={i}>
            <span className="font-mono text-xs">{o.url}</span>
            {o.purpose && (
              <span className="text-xs text-muted-foreground"> — {o.purpose}</span>
            )}
          </li>
        ))}
      </ul>
    </Section>
  );
}

function CannotBlock({ cannot }: { cannot: string[] }) {
  const { t } = useTranslation();
  return (
    <Section title={t("settings.appInstall.cannot")}>
      <ul className="space-y-1 text-[13px] text-muted-foreground">
        {cannot.map((line, i) => (
          <li key={i} className="flex items-start gap-2">
            <Lock className="w-3 h-3 shrink-0 mt-0.5" />
            {line}
          </li>
        ))}
      </ul>
    </Section>
  );
}

function CspBlock({ csp }: { csp: string }) {
  const { t } = useTranslation();
  return (
    <Section title={t("settings.appInstall.csp")}>
      <div className="font-mono text-2xs text-muted-foreground bg-muted/40 rounded p-2 break-all leading-relaxed">
        {csp}
      </div>
    </Section>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-border pt-4">
      <div className="text-2xs font-semibold text-muted-foreground mb-2">
        {title}
      </div>
      {children}
    </div>
  );
}

function ManifestErrors({ errors }: { errors: string[] }) {
  const { t } = useTranslation();
  return (
    <div className="rounded-md border border-red-500/30 bg-red-500/5 p-3">
      <div className="text-xs font-semibold text-red-600 dark:text-red-400 mb-1.5">
        {t("settings.appInstall.manifestErrors")}
      </div>
      <ul className="text-[13px] space-y-1">
        {errors.map((e, i) => (
          <li key={i} className="flex items-start gap-2">
            <span className="text-red-500 shrink-0">•</span>
            <span className="font-mono text-xs">{e}</span>
          </li>
        ))}
      </ul>
      <div className="text-xs text-muted-foreground mt-2">
        {t("settings.appInstall.installDisabled")}
      </div>
    </div>
  );
}

function ErrorBlock({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-red-500/30 bg-red-500/5 p-3 text-[13px] text-red-700 dark:text-red-400">
      {message}
    </div>
  );
}
