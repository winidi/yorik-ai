/**
 * Settings → Maintenance → Hosted families (the owner only, host only).
 *
 * The operator's "I want to invite my mom" UI. Lists every tenant on
 * this box, surfaces the current invite status per tenant, and lets
 * the host admin spin up new tenants with a copy-paste invite link.
 *
 * Backend contract:
 *   GET    /api/tenants                → list of { name, port, invite }
 *   POST   /api/tenants                → create tenant, return invite_url
 *   DELETE /api/tenants/{name}         → drop tenant + clean upstream users
 *
 * This component is hidden from tenant Yoriks (the endpoint refuses
 * with 400 when YORIK_DB_NAME != 'postgres'; we surface that as a
 * read-only "not available" panel rather than crashing).
 */

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { Loader2, Plus, Copy, Trash2, Check, AlertTriangle, KeyRound, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { api, ApiError } from "@/lib/api";
import { locale } from "@/i18n";

interface InviteRow {
  token:         string;
  tenant_name:   string;
  port:          number;
  display_label: string | null;
  expires_at:    string;
  consumed_at:   string | null;
  created_at:    string;
}

interface TenantRow {
  name:   string;
  port:   number | null;
  invite: InviteRow | null;
}

interface CreateResponse {
  tenant_name:   string;
  port:          number;
  invite_url:    string;
  invite_token:  string;
  expires_at:    string;
  display_label: string | null;
  /** false when the family's Yorik didn't start (the link then leads nowhere yet). */
  unit_started?: boolean;
  unit_warning?: string | null;
}

interface Props {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}

export function HouseholdsTab({ toast }: Props) {
  const { t } = useTranslation();
  const [tenants, setTenants] = useState<TenantRow[] | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [createdInvite, setCreatedInvite] = useState<CreateResponse | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await api.get<TenantRow[]>("/api/tenants");
      setTenants(r);
      setUnavailable(null);
    } catch (err: any) {
      // 400 = "this Yorik is itself a tenant" → render the unavailable
      // panel rather than spamming an error toast.
      if (err instanceof ApiError && err.status === 400 &&
          /itself a tenant/i.test(err.message)) {
        setUnavailable(err.message);
        setTenants([]);
        return;
      }
      const msg = err instanceof ApiError ? err.message : String(err);
      toast(t("settings.households.loadFailed", { msg }), "error");
    }
  }, [toast, t]);

  useEffect(() => { refresh(); }, [refresh]);

  if (unavailable) {
    return (
      <div className="max-w-2xl">
        <div className="rounded-xl border border-border bg-card p-6">
          <div className="flex items-start gap-3">
            <AlertTriangle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
            <div>
              <h2 className="font-semibold mb-1">{t("settings.households.unavailableTitle")}</h2>
              <p className="text-sm text-muted-foreground">
                {unavailable}
              </p>
              <p className="text-sm text-muted-foreground mt-2">
                {t("settings.households.unavailableHint")}
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-3xl space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold">{t("settings.households.title")}</h2>
          <p className="text-sm text-muted-foreground mt-1">
            {t("settings.households.intro")}
          </p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="inline-flex items-center gap-2 px-3.5 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:opacity-90 shrink-0"
        >
          <Plus className="w-4 h-4" /> {t("settings.households.add")}
        </button>
      </header>

      {createdInvite && (
        <InvitePanel
          created={createdInvite}
          onDismiss={() => { setCreatedInvite(null); refresh(); }}
          toast={toast}
        />
      )}

      {tenants === null ? (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin mr-2" /> {t("settings.households.loading")}
        </div>
      ) : tenants.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          {t("settings.households.empty")}
        </div>
      ) : (
        <div className="space-y-2">
          {tenants.map(t => (
            <TenantCard
              key={t.name}
              tenant={t}
              onRefresh={refresh}
              toast={toast}
            />
          ))}
        </div>
      )}

      {showCreate && (
        <CreateHouseholdModal
          onCancel={() => setShowCreate(false)}
          onCreated={result => {
            setShowCreate(false);
            setCreatedInvite(result);
            if (result.unit_started === false || result.unit_warning) {
              toast(t("settings.households.createdNotStarted", { name: result.tenant_name, reason: result.unit_warning || t("settings.households.unknownReason") }), "error");
            } else {
              toast(t("settings.households.created", { name: result.tenant_name }), "success");
            }
          }}
          toast={toast}
        />
      )}
    </div>
  );
}

// ───────────────────────── invite panel (post-create) ─────────────────────

function InvitePanel({
  created, onDismiss, toast,
}: {
  created: CreateResponse;
  onDismiss: () => void;
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const copy = useCallback(() => {
    navigator.clipboard.writeText(created.invite_url).then(
      () => {
        setCopied(true);
        toast(t("settings.households.inviteCopied"), "success");
        setTimeout(() => setCopied(false), 2500);
      },
      () => toast(t("settings.households.copyFailed"), "error"),
    );
  }, [created.invite_url, toast, t]);

  return (
    <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-5">
      <div className="flex items-start justify-between gap-4 mb-3">
        <div>
          <h3 className="font-semibold">
            {t("settings.households.inviteLinkFor", { name: created.display_label || created.tenant_name })}
          </h3>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("settings.households.oneTimeExpires", { when: new Date(created.expires_at).toLocaleString(locale()) })}
          </p>
        </div>
        <button
          onClick={onDismiss}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          {t("settings.households.dismiss")}
        </button>
      </div>
      <div className="flex items-stretch gap-2">
        <input
          readOnly
          value={created.invite_url}
          className="flex-1 px-3 py-2 text-sm font-mono bg-background border border-border rounded-lg select-all"
          onClick={e => (e.target as HTMLInputElement).select()}
        />
        <button
          onClick={copy}
          className={cn(
            "inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg border",
            copied
              ? "border-emerald-500 bg-emerald-500/10 text-emerald-600"
              : "border-border bg-background hover:bg-muted",
          )}
        >
          {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
          {copied ? t("settings.households.copied") : t("settings.households.copy")}
        </button>
      </div>
      <p className="text-xs text-muted-foreground mt-3">
        {t("settings.households.inviteHint")}
      </p>
    </div>
  );
}

// ───────────────────────── per-tenant row ─────────────────────────────────

function TenantCard({
  tenant, onRefresh, toast,
}: {
  tenant: TenantRow;
  onRefresh: () => void;
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [confirmDrop, setConfirmDrop] = useState(false);
  const [resetEmail, setResetEmail] = useState<string | null>(null);
  const [resetResult, setResetResult] = useState<{ invite_url: string; target_email: string; expires_at: string } | null>(null);

  const issueReset = useCallback(async (email: string) => {
    setBusy(true);
    try {
      const r = await api.post<{
        invite_url: string;
        target_email: string;
        expires_at: string;
      }>(`/api/tenants/${encodeURIComponent(tenant.name)}/issue-reset-invite`, {
        target_email: email,
      });
      setResetResult(r);
      setResetEmail(null);
    } catch (err: any) {
      const msg = err instanceof ApiError ? err.message : String(err);
      toast(t("settings.households.resetFailed", { msg }), "error");
    } finally {
      setBusy(false);
    }
  }, [tenant.name, toast, t]);

  const drop = useCallback(async () => {
    setBusy(true);
    try {
      await api.delete(`/api/tenants/${encodeURIComponent(tenant.name)}`);
      toast(t("settings.households.dropped", { name: tenant.name }), "success");
      onRefresh();
    } catch (err: any) {
      const msg = err instanceof ApiError ? err.message : String(err);
      toast(t("settings.households.dropFailed", { msg }), "error");
    } finally {
      setBusy(false);
      setConfirmDrop(false);
    }
  }, [tenant.name, toast, onRefresh, t]);

  const inviteStatus = tenant.invite
    ? tenant.invite.consumed_at
      ? { label: t("settings.households.status.active"), color: "text-emerald-600 bg-emerald-500/10" }
      : new Date(tenant.invite.expires_at) < new Date()
        ? { label: t("settings.households.status.expired"), color: "text-muted-foreground bg-muted/40" }
        : { label: t("settings.households.status.pending"), color: "text-amber-600 bg-amber-500/10" }
    : { label: t("settings.households.status.none"), color: "text-muted-foreground bg-muted/40" };

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium truncate">
              {tenant.invite?.display_label || tenant.name}
            </span>
            <span className={cn("text-2xs font-medium px-1.5 py-0.5 rounded", inviteStatus.color)}>
              {inviteStatus.label}
            </span>
          </div>
          <div className="text-xs text-muted-foreground mt-1 font-mono">
            yorik_tenant_{tenant.name}
            {tenant.port != null && <> · {t("settings.households.port", { port: tenant.port })}</>}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => setResetEmail("")}
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground px-2 py-1"
            title={t("settings.households.resetTitle")}
          >
            <KeyRound className="w-3.5 h-3.5" /> {t("settings.households.reset")}
          </button>
          <button
            onClick={() => setConfirmDrop(true)}
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-red-500 px-2 py-1"
            title={t("settings.households.dropTitle")}
          >
            <Trash2 className="w-3.5 h-3.5" /> {t("settings.households.drop")}
          </button>
        </div>
      </div>
      {resetEmail !== null && (
        <ResetEmailPrompt
          tenant={tenant}
          busy={busy}
          initial={resetEmail}
          onCancel={() => setResetEmail(null)}
          onSubmit={issueReset}
        />
      )}
      {resetResult && (
        <ResetInvitePanel
          result={resetResult}
          onDismiss={() => setResetResult(null)}
          toast={toast}
        />
      )}
      {confirmDrop && (
        <DropTenantConfirmModal
          tenant={tenant}
          busy={busy}
          onClose={() => setConfirmDrop(false)}
          onConfirm={drop}
        />
      )}
    </div>
  );
}

function DropTenantConfirmModal({
  tenant, busy, onClose, onConfirm,
}: {
  tenant: TenantRow;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation();
  const [typed, setTyped] = useState("");
  const matches = typed.trim() === tenant.name;
  return createPortal(
    <div
      className="fixed inset-0 z-[810] flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
      onClick={busy ? undefined : onClose}
    >
      <div
        className="w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="font-semibold text-red-600 dark:text-red-400">{t("settings.households.dropModal.title")}</div>
          <button
            onClick={onClose}
            disabled={busy}
            className="p-1.5 hover:bg-muted rounded-md text-muted-foreground disabled:opacity-50"
            aria-label={t("settings.households.dropModal.close")}
          >
            <X className="w-4 h-4" />
          </button>
        </header>
        <div className="p-5 space-y-4">
          <div className="text-sm leading-relaxed">
            {t("settings.households.dropModal.warnBefore")}<strong className="text-red-600 dark:text-red-400">{t("settings.households.dropModal.warnStrong")}</strong>{t("settings.households.dropModal.warnDb")}{" "}
            <span className="font-mono text-xs">yorik_tenant_{tenant.name}</span>{t("settings.households.dropModal.warnAfter")}
          </div>
          <div>
            <label className="text-xs text-muted-foreground block mb-1.5">
              {t("settings.households.dropModal.typeBefore")}<span className="font-mono text-foreground">{tenant.name}</span>{t("settings.households.dropModal.typeAfter")}
            </label>
            <input
              autoFocus
              value={typed}
              onChange={e => setTyped(e.target.value)}
              placeholder={tenant.name}
              disabled={busy}
              className="w-full px-3 py-2 text-sm font-mono bg-background border border-border rounded-md"
            />
          </div>
          <div className="flex gap-2 pt-1">
            <button
              onClick={onClose}
              disabled={busy}
              className="flex-1 px-3 py-2 rounded-md text-sm font-medium border border-border hover:bg-muted transition disabled:opacity-50"
            >
              {t("settings.households.cancel")}
            </button>
            <button
              onClick={onConfirm}
              disabled={busy || !matches}
              className={cn(
                "flex-1 px-3 py-2 rounded-md text-sm font-medium transition",
                matches
                  ? "bg-red-500 text-white hover:bg-red-600"
                  : "bg-muted text-muted-foreground cursor-not-allowed",
                busy && "opacity-50 cursor-wait",
              )}
            >
              {busy ? t("settings.households.dropModal.dropping") : t("settings.households.dropModal.confirm")}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

function ResetEmailPrompt({
  tenant, busy, initial, onCancel, onSubmit,
}: {
  tenant: TenantRow;
  busy: boolean;
  initial: string;
  onCancel: () => void;
  onSubmit: (email: string) => void;
}) {
  const { t } = useTranslation();
  const [email, setEmail] = useState(initial);
  return (
    <form
      onSubmit={e => { e.preventDefault(); if (email.trim()) onSubmit(email.trim().toLowerCase()); }}
      className="mt-3 flex items-center gap-2 pt-3 border-t border-border"
    >
      <input
        autoFocus
        type="email"
        value={email}
        onChange={e => setEmail(e.target.value)}
        placeholder={t("settings.households.adminEmailIn", { name: tenant.name })}
        className="flex-1 px-3 py-1.5 text-sm bg-background border border-border rounded-md"
        required
      />
      <button
        type="submit"
        disabled={busy || !email.trim()}
        className="text-xs font-medium px-2.5 py-1.5 rounded bg-amber-500 text-white hover:bg-amber-600 disabled:opacity-50"
      >
        {busy ? t("settings.households.issuing") : t("settings.households.issueReset")}
      </button>
      <button
        type="button"
        onClick={onCancel}
        disabled={busy}
        className="text-xs px-2.5 py-1.5 rounded border border-border hover:bg-muted"
      >
        {t("settings.households.cancel")}
      </button>
    </form>
  );
}

function ResetInvitePanel({
  result, onDismiss, toast,
}: {
  result: { invite_url: string; target_email: string; expires_at: string };
  onDismiss: () => void;
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const copy = useCallback(() => {
    navigator.clipboard.writeText(result.invite_url).then(
      () => { setCopied(true); toast(t("settings.households.resetLinkCopied"), "success"); setTimeout(() => setCopied(false), 2500); },
      () => toast(t("settings.households.copyFailedShort"), "error"),
    );
  }, [result.invite_url, toast, t]);
  return (
    <div className="mt-3 pt-3 border-t border-amber-500/30 bg-amber-500/5 rounded-md p-3">
      <div className="text-xs font-medium text-amber-700 dark:text-amber-400 mb-2">
        {t("settings.households.resetLinkFor", { email: result.target_email, when: new Date(result.expires_at).toLocaleString(locale()) })}
      </div>
      <div className="flex items-stretch gap-2">
        <input
          readOnly
          value={result.invite_url}
          onClick={e => (e.target as HTMLInputElement).select()}
          className="flex-1 px-2.5 py-1.5 text-xs font-mono bg-background border border-border rounded-md select-all"
        />
        <button
          onClick={copy}
          className={cn(
            "inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-md border",
            copied
              ? "border-emerald-500 bg-emerald-500/10 text-emerald-600"
              : "border-border bg-background hover:bg-muted",
          )}
        >
          {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
          {copied ? t("settings.households.copied") : t("settings.households.copy")}
        </button>
        <button
          onClick={onDismiss}
          className="text-xs px-2 py-1.5 text-muted-foreground hover:text-foreground"
        >
          {t("settings.households.dismiss")}
        </button>
      </div>
    </div>
  );
}

// ───────────────────────── create-household modal ─────────────────────────

function CreateHouseholdModal({
  onCancel, onCreated, toast,
}: {
  onCancel: () => void;
  onCreated: (r: CreateResponse) => void;
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [displayLabel, setDisplayLabel] = useState("");
  const [expiresHours, setExpiresHours] = useState(168);
  const [busy, setBusy] = useState(false);

  const submit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    const slug = name.trim().toLowerCase();
    if (!/^[a-z][a-z0-9_]{0,23}$/.test(slug)) {
      toast(t("settings.households.create.nameRule"), "error");
      return;
    }
    setBusy(true);
    try {
      const r = await api.post<CreateResponse>("/api/tenants", {
        name: slug,
        display_label: displayLabel.trim() || null,
        invite_expires_hours: expiresHours,
      });
      onCreated(r);
    } catch (err: any) {
      const msg = err instanceof ApiError ? err.message : String(err);
      toast(t("settings.households.create.failed", { msg }), "error");
    } finally {
      setBusy(false);
    }
  }, [name, displayLabel, expiresHours, onCreated, toast, t]);

  return (
    <div className="fixed inset-0 bg-black/40 z-[800] flex items-center justify-center p-4">
      <div className="bg-card rounded-xl border border-border shadow-xl max-w-md w-full max-h-full overflow-y-auto">
        <form onSubmit={submit}>
          <div className="px-5 py-4 border-b border-border">
            <h3 className="font-semibold">{t("settings.households.add")}</h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              {t("settings.households.create.intro")}
            </p>
          </div>
          <div className="px-5 py-4 space-y-4">
            <label className="block">
              <span className="text-xs font-medium text-muted-foreground">{t("settings.households.create.slug")}</span>
              <input
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder={t("settings.households.create.slugPlaceholder")}
                className="mt-1 w-full px-3 py-2 text-sm bg-background border border-border rounded-lg font-mono"
                autoFocus
                required
              />
              <span className="block text-xs text-muted-foreground mt-1">
                {t("settings.households.create.slugHintBefore")}<code>yorik_tenant_{name || "<slug>"}</code>{t("settings.households.create.slugHintAfter")}
              </span>
            </label>
            <label className="block">
              <span className="text-xs font-medium text-muted-foreground">{t("settings.households.create.label")}</span>
              <input
                value={displayLabel}
                onChange={e => setDisplayLabel(e.target.value)}
                placeholder={t("settings.households.create.labelPlaceholder")}
                className="mt-1 w-full px-3 py-2 text-sm bg-background border border-border rounded-lg"
              />
              <span className="block text-xs text-muted-foreground mt-1">
                {t("settings.households.create.labelHint")}
              </span>
            </label>
            <label className="block">
              <span className="text-xs font-medium text-muted-foreground">{t("settings.households.create.expires")}</span>
              <select
                value={expiresHours}
                onChange={e => setExpiresHours(Number(e.target.value))}
                className="mt-1 w-full px-3 py-2 text-sm bg-background border border-border rounded-lg"
              >
                <option value={24}>{t("settings.households.create.hours", { count: 24 })}</option>
                <option value={72}>{t("settings.households.create.days", { count: 3 })}</option>
                <option value={168}>{t("settings.households.create.days", { count: 7 })}</option>
                <option value={336}>{t("settings.households.create.days", { count: 14 })}</option>
                <option value={720}>{t("settings.households.create.days", { count: 30 })}</option>
              </select>
            </label>
          </div>
          <div className="px-5 py-3 border-t border-border bg-muted/30 flex justify-end gap-2 rounded-b-xl">
            <button
              type="button"
              onClick={onCancel}
              disabled={busy}
              className="px-3 py-1.5 text-sm rounded-lg border border-border hover:bg-muted disabled:opacity-50"
            >
              {t("settings.households.cancel")}
            </button>
            <button
              type="submit"
              disabled={busy || !name.trim()}
              className="px-3 py-1.5 text-sm rounded-lg bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50 inline-flex items-center gap-2"
            >
              {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              {busy ? t("settings.households.create.creating") : t("settings.households.create.submit")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
