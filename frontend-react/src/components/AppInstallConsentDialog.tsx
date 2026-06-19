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
            ? e.message || `Preflight failed (HTTP ${e.status})`
            : "Preflight request failed",
        );
      }
    })();
    return () => { cancelled = true; };
  }, [manifest, sourceDir]);

  const handleInstall = useCallback(async () => {
    if (!sourceDir) {
      setInstallErr("No source directory — preview mode only.");
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
          ? e.message || `Install failed (HTTP ${e.status})`
          : "Install request failed",
      );
      setInstalling(false);
    }
  }, [sourceDir, data?.summary, onInstalled, onClose]);

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
            {data ? `Install ${data.summary.app.name ?? "app"}?` : "Loading…"}
          </div>
          <button
            onClick={onClose}
            className="p-1.5 hover:bg-muted rounded-md text-muted-foreground"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="p-5 space-y-4 overflow-y-auto">
          {loadErr && <ErrorBlock message={loadErr} />}

          {!data && !loadErr && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground py-8 justify-center">
              <Loader2 className="w-4 h-4 animate-spin" />
              Reading manifest…
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
            Cancel
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
                Installing…
              </>
            ) : (
              <>
                <Download className="w-3.5 h-3.5" />
                Install
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
            {app.author && <>by {app.author}</>}
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
                  source
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
  return (
    <Section title="Its own data">
      <div className="text-[13px] flex items-start gap-2">
        <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
        <span>
          Private database tables in{" "}
          <span className="font-mono text-[11px] text-muted-foreground">
            {schema}
          </span>
          : {tables.map((t, i) => (
            <span key={t}>
              <span className="font-mono text-[11px] text-muted-foreground">
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
  return (
    <Section title="It wants to">
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
  if (scope.kind === "read") {
    return (
      <>
        <strong>Read</strong> your{" "}
        <span className="font-mono text-[11px]">{scope.table}</span>
        {scope.columns.length > 0 && (
          <>
            {" "}(<span className="text-muted-foreground">
              {scope.columns.join(", ")}
            </span>)
          </>
        )}
        {scope.purpose && (
          <div className="text-xs text-muted-foreground mt-0.5">
            Why: {scope.purpose}
          </div>
        )}
      </>
    );
  }
  if (scope.kind === "write_rejected") {
    return (
      <>
        <strong className="text-red-600 dark:text-red-400">Write</strong> to{" "}
        <span className="font-mono text-[11px]">{scope.table}</span>{" "}
        <span className="text-xs text-muted-foreground">
          (not allowed in v1 — declared but ignored)
        </span>
      </>
    );
  }
  if (scope.kind === "skill") {
    return (
      <>
        Ask Yorik to run{" "}
        <span className="font-mono text-[11px]">{scope.skill}</span>
      </>
    );
  }
  if (scope.kind === "connector") {
    return (
      <>
        Use the{" "}
        <span className="font-mono text-[11px]">{scope.connector}</span>{" "}
        connector
      </>
    );
  }
  if (scope.kind === "realtime") {
    return (
      <>
        Get realtime updates for{" "}
        <span className="font-mono text-[11px]">{scope.table}</span>
      </>
    );
  }
  if (scope.kind === "scheduled") {
    return (
      <>
        Run <span className="font-mono text-[11px]">{scope.invokes}</span>{" "}
        on schedule{" "}
        <span className="text-muted-foreground">({scope.cron})</span>
        {scope.purpose && (
          <div className="text-xs text-muted-foreground mt-0.5">
            Why: {scope.purpose}
          </div>
        )}
      </>
    );
  }
  // webhook
  return (
    <>
      Expose webhook at{" "}
      <span className="font-mono text-[11px]">{scope.path}</span>
      {scope.purpose && (
        <div className="text-xs text-muted-foreground mt-0.5">
          Why: {scope.purpose}
        </div>
      )}
    </>
  );
}

function NetworkBlock({ network }: { network: PreflightSummary["network"] }) {
  if (network.talks_only_to_yorik) {
    return (
      <Section title="Network">
        <div className="flex items-start gap-2 text-[13px]">
          <ShieldCheck className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
          <span>This app only talks to your Yorik box.</span>
        </div>
      </Section>
    );
  }
  return (
    <Section title="Network">
      <div className="text-[13px] mb-1.5 flex items-start gap-2">
        <Globe className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
        <span>This app may also contact:</span>
      </div>
      <ul className="space-y-1 text-[13px] ml-5">
        {network.outbound.map((o, i) => (
          <li key={i}>
            <span className="font-mono text-[11px]">{o.url}</span>
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
  return (
    <Section title="This app cannot">
      <ul className="space-y-1 text-[12.5px] text-muted-foreground">
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
  return (
    <Section title="Browser sandbox (Content-Security-Policy)">
      <div className="font-mono text-[10px] text-muted-foreground bg-muted/40 rounded p-2 break-all leading-relaxed">
        {csp}
      </div>
    </Section>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-border pt-4">
      <div className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground mb-2">
        {title}
      </div>
      {children}
    </div>
  );
}

function ManifestErrors({ errors }: { errors: string[] }) {
  return (
    <div className="rounded-md border border-red-500/30 bg-red-500/5 p-3">
      <div className="text-[11px] uppercase tracking-wider font-semibold text-red-600 dark:text-red-400 mb-1.5">
        Manifest errors
      </div>
      <ul className="text-[13px] space-y-1">
        {errors.map((e, i) => (
          <li key={i} className="flex items-start gap-2">
            <span className="text-red-500 shrink-0">•</span>
            <span className="font-mono text-[11px]">{e}</span>
          </li>
        ))}
      </ul>
      <div className="text-xs text-muted-foreground mt-2">
        Install is disabled while errors are present.
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
