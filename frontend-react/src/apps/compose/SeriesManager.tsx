/**
 * Document-numbering manager — modal that owns the entire series lifecycle.
 *
 * Two views inside the modal:
 *   1. List view: existing series + create new + edit / set-next-number.
 *      If zero series exist, shows the regional-preset wizard instead
 *      (DE / US / PL / Custom). One-click installs a country's default
 *      set.
 *   2. Allocations view (per series): the audit trail. Every number ever
 *      consumed, with timestamp, title, and Paperless link. This is the
 *      "Steuerprüfungs-tauglich" proof.
 *
 * The whole feature stays invisible in Compose until the user opens this
 * — that's the "plugin feel" without needing the extension framework.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Loader2, X, Plus, Hash, RefreshCw, Trash2, Edit3,
  ChevronRight, FileText, CheckCircle2, AlertCircle,
  History, Wand2, ExternalLink,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import type {
  DocumentSeries, SeriesAllocation, SeriesPreset,
} from "./types";

type Toast = (text: string, kind?: "info" | "success" | "error") => void;

interface Props {
  onClose: () => void;
  onChanged: () => void; // ping parent to refresh draft (numbering may have changed)
  toast: Toast;
  initialKind?: string; // when opened from a specific numbered arg, focus on that kind
  // When true: render inline as a card without the fixed-position overlay,
  // backdrop, or X-close button — for embedding in Settings → Numbering
  // alongside the other settings tabs. Default false keeps the modal
  // behaviour Compose relies on.
  inline?: boolean;
}

export function SeriesManager({ onClose, onChanged, toast, initialKind, inline = false }: Props) {
  const [series, setSeries] = useState<DocumentSeries[] | null>(null);
  const [presets, setPresets] = useState<Record<string, SeriesPreset>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [allocationsFor, setAllocationsFor] = useState<DocumentSeries | null>(null);
  const [editing, setEditing] = useState<DocumentSeries | null>(null);
  const [creating, setCreating] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [s, p] = await Promise.all([
        api.get<DocumentSeries[]>("/api/compose/series?role=admin"),
        api.get<{ presets: Record<string, SeriesPreset> }>("/api/compose/series/presets?role=admin"),
      ]);
      setSeries(s);
      setPresets(p.presets);
    } catch (e: any) {
      toast(`Could not load numbering: ${e.message}`, "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    function esc(e: KeyboardEvent) { if (e.key === "Escape" && !busy) onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose, busy]);

  async function installPreset(key: string) {
    setBusy(true);
    try {
      const r = await api.post<{ created: DocumentSeries[]; count: number }>(
        "/api/compose/series/install-preset?role=admin",
        { preset: key },
      );
      toast(`Installed ${r.count} series for ${presets[key]?.label}`, "success");
      await refresh();
      onChanged();
    } catch (e: any) {
      toast(`Install failed: ${e.message}`, "error");
    } finally {
      setBusy(false);
    }
  }

  async function deleteSeries(s: DocumentSeries) {
    if (!confirm(`Delete series "${s.name}"? Only allowed if it has no allocations.`)) return;
    setBusy(true);
    try {
      await api.delete(`/api/compose/series/${s.id}?role=admin`);
      toast(`Deleted "${s.name}"`, "success");
      await refresh();
      onChanged();
    } catch (e: any) {
      toast(`Delete refused: ${e.message}`, "error");
    } finally {
      setBusy(false);
    }
  }

  const isEmpty = !loading && (series || []).length === 0;

  const card = (
    <div
      className={cn(
        "bg-card border border-border rounded-2xl flex flex-col overflow-hidden",
        inline
          ? "w-full shadow-sm"
          : "w-full max-w-3xl max-h-[85vh] shadow-2xl",
      )}
      onClick={e => e.stopPropagation()}
    >
      {!inline && (
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Hash className="w-4 h-4 text-rose-500" />
            <span className="font-semibold">Document numbering</span>
            {initialKind && (
              <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full bg-rose-500/10 text-rose-500">
                {initialKind}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            className="w-7 h-7 rounded-md hover:bg-muted text-muted-foreground flex items-center justify-center disabled:opacity-50"
          >
            <X className="w-4 h-4" />
          </button>
        </header>
      )}

      {/* Content. flex-1 in modal mode (card grows to max-h), natural
          height inline (parent page scrolls). */}
      <div className={cn("overflow-y-auto p-5", inline ? "" : "flex-1")}>
        {loading && (
          <div className="flex items-center justify-center py-16 text-muted-foreground">
            <Loader2 className="w-5 h-5 animate-spin" />
          </div>
        )}

        {!loading && allocationsFor && (
          <AllocationsView
            series={allocationsFor}
            onBack={() => setAllocationsFor(null)}
            toast={toast}
          />
        )}

        {!loading && !allocationsFor && isEmpty && (
          <WizardView
            presets={presets}
            onInstall={installPreset}
            onCustom={() => setCreating(true)}
            busy={busy}
          />
        )}

        {!loading && !allocationsFor && !isEmpty && (
          <ListView
            series={series || []}
            onCreate={() => setCreating(true)}
            onEdit={setEditing}
            onDelete={deleteSeries}
            onShowAllocations={setAllocationsFor}
            busy={busy}
          />
        )}
      </div>

      {!loading && !allocationsFor && (
        <footer className="px-5 py-3 border-t border-border bg-muted/20 text-[11px] text-muted-foreground flex items-center justify-between">
          <span className="flex items-center gap-1.5">
            <AlertCircle className="w-3 h-3" />
            Numbers are consumed only when you Save to Paperless or Send — drafts never burn a number.
          </span>
          <button
            onClick={refresh}
            className="text-muted-foreground hover:text-foreground transition"
            title="Reload"
          >
            <RefreshCw className={cn("w-3 h-3", loading && "animate-spin")} />
          </button>
        </footer>
      )}
    </div>
  );

  // Editor sub-modals open on top of the card regardless of inline/modal mode.
  const dialogs = (
    <>
      {creating && (
        <SeriesEditor
          mode="create"
          onClose={() => setCreating(false)}
          onSaved={async () => {
            setCreating(false);
            await refresh();
            onChanged();
          }}
          toast={toast}
        />
      )}
      {editing && (
        <SeriesEditor
          mode="edit"
          existing={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await refresh();
            onChanged();
          }}
          toast={toast}
        />
      )}
    </>
  );

  if (inline) {
    return <>{card}{dialogs}</>;
  }

  return (
    <div
      className="fixed inset-0 z-[1000] bg-black/60 backdrop-blur-sm flex items-center justify-center p-6"
      onClick={() => { if (!busy) onClose(); }}
    >
      {card}
      {dialogs}
    </div>
  );
}

// ───────────────────────── First-run wizard ─────────────────────────────

function WizardView({
  presets, onInstall, onCustom, busy,
}: {
  presets: Record<string, SeriesPreset>;
  onInstall: (key: string) => void;
  onCustom: () => void;
  busy: boolean;
}) {
  const entries = Object.entries(presets);
  return (
    <div className="space-y-5">
      <div className="text-center max-w-md mx-auto py-4">
        <div className="w-12 h-12 mx-auto mb-3 rounded-2xl bg-gradient-to-br from-rose-500/20 to-violet-500/20 flex items-center justify-center">
          <Wand2 className="w-5 h-5 text-rose-500" />
        </div>
        <div className="font-semibold mb-1">Set up document numbering</div>
        <div className="text-sm text-muted-foreground leading-relaxed">
          Invoices and quotes need sequential numbers — required by law in Germany & Poland,
          best practice everywhere else. Pick a regional preset, or build your own series.
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {entries.map(([key, p]) => (
          <button
            key={key}
            onClick={() => onInstall(key)}
            disabled={busy}
            className={cn(
              "text-left p-4 rounded-xl border border-border bg-card hover:border-rose-500/40",
              "hover:shadow-md transition disabled:opacity-50",
            )}
          >
            <div className="font-medium mb-1">{p.label}</div>
            <div className="text-xs text-muted-foreground leading-relaxed mb-2">
              {p.description}
            </div>
            <div className="flex flex-wrap gap-1">
              {p.series.map(s => (
                <span
                  key={s.kind}
                  className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-muted/60 text-foreground/70"
                >
                  {s.kind}
                </span>
              ))}
            </div>
          </button>
        ))}
        <button
          onClick={onCustom}
          disabled={busy}
          className={cn(
            "text-left p-4 rounded-xl border-2 border-dashed border-border hover:border-rose-500/40",
            "hover:bg-muted/30 transition disabled:opacity-50",
          )}
        >
          <div className="font-medium mb-1 flex items-center gap-2">
            <Plus className="w-3.5 h-3.5" /> Custom
          </div>
          <div className="text-xs text-muted-foreground leading-relaxed">
            Build one series from scratch. Pick your own kind, format, and starting number.
          </div>
        </button>
      </div>
    </div>
  );
}

// ───────────────────────── List view ────────────────────────────────────

function ListView({
  series, onCreate, onEdit, onDelete, onShowAllocations, busy,
}: {
  series: DocumentSeries[];
  onCreate: () => void;
  onEdit: (s: DocumentSeries) => void;
  onDelete: (s: DocumentSeries) => void;
  onShowAllocations: (s: DocumentSeries) => void;
  busy: boolean;
}) {
  // Group by kind so visually it's clear which kind owns which series.
  const groups = useMemo(() => {
    const map = new Map<string, DocumentSeries[]>();
    for (const s of series) {
      const list = map.get(s.kind);
      if (list) list.push(s);
      else map.set(s.kind, [s]);
    }
    return Array.from(map.entries());
  }, [series]);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div className="text-sm text-muted-foreground">
          {series.length} series across {groups.length} kind{groups.length === 1 ? "" : "s"}
        </div>
        <button
          onClick={onCreate}
          disabled={busy}
          className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-rose-500 hover:bg-rose-600 text-white font-medium shadow-sm transition disabled:opacity-50"
        >
          <Plus className="w-3.5 h-3.5" /> New series
        </button>
      </div>

      {groups.map(([kind, group]) => (
        <section key={kind}>
          <h4 className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
            {kind}
          </h4>
          <div className="space-y-2">
            {group.map(s => (
              <SeriesRow
                key={s.id}
                series={s}
                onEdit={() => onEdit(s)}
                onDelete={() => onDelete(s)}
                onShowAllocations={() => onShowAllocations(s)}
                busy={busy}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function SeriesRow({
  series, onEdit, onDelete, onShowAllocations, busy,
}: {
  series: DocumentSeries;
  onEdit: () => void;
  onDelete: () => void;
  onShowAllocations: () => void;
  busy: boolean;
}) {
  return (
    <div className="bg-card border border-border rounded-xl p-3 hover:border-rose-500/40 transition">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="font-medium text-sm">{series.name}</span>
            {series.is_default && (
              <span className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600">
                Default
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span className="text-muted-foreground">Next:</span>
            <code className="font-mono text-foreground bg-muted/50 px-1.5 py-0.5 rounded">
              {series.preview?.formatted || "—"}
            </code>
            {series.year_reset && (
              <span className="text-[10px] text-muted-foreground">↻ resets each year</span>
            )}
          </div>
          <div className="text-[10px] text-muted-foreground mt-1 font-mono">
            scheme: {series.scheme} · padding: {series.seq_padding}
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={onShowAllocations}
            disabled={busy}
            title="View audit trail"
            className="w-7 h-7 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-50 flex items-center justify-center"
          >
            <History className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={onEdit}
            disabled={busy}
            title="Edit"
            className="w-7 h-7 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-50 flex items-center justify-center"
          >
            <Edit3 className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={onDelete}
            disabled={busy}
            title="Delete"
            className="w-7 h-7 rounded-md hover:bg-red-500/10 text-muted-foreground hover:text-red-500 transition disabled:opacity-50 flex items-center justify-center"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}

// ───────────────────────── Editor (create / edit) ───────────────────────

function SeriesEditor({
  mode, existing, onClose, onSaved, toast,
}: {
  mode: "create" | "edit";
  existing?: DocumentSeries;
  onClose: () => void;
  onSaved: () => void;
  toast: Toast;
}) {
  const [kind, setKind] = useState(existing?.kind || "");
  const [name, setName] = useState(existing?.name || "");
  const [scheme, setScheme] = useState(existing?.scheme || "{year}-{seq}");
  const [prefix, setPrefix] = useState(existing?.prefix || "");
  const [seqPadding, setSeqPadding] = useState(existing?.seq_padding || 3);
  const [startingOrNext, setStartingOrNext] = useState(
    mode === "create" ? 1 : (existing?.next_number || 1),
  );
  const [yearReset, setYearReset] = useState(existing?.year_reset ?? true);
  const [isDefault, setIsDefault] = useState(existing?.is_default ?? true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    function esc(e: KeyboardEvent) { if (e.key === "Escape" && !busy) onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose, busy]);

  // Live preview of what the next number would look like with the current settings.
  const previewExample = useMemo(() => {
    const year = new Date().getFullYear();
    const seqStr = String(startingOrNext).padStart(Math.max(1, seqPadding), "0");
    return scheme
      .replace("{prefix}", prefix || "")
      .replace("{year}", String(year))
      .replace("{seq}", seqStr);
  }, [scheme, prefix, seqPadding, startingOrNext]);

  async function save() {
    setBusy(true);
    try {
      if (mode === "create") {
        await api.post("/api/compose/series?role=admin", {
          kind: kind.trim(),
          name: name.trim(),
          scheme: scheme.trim(),
          prefix,
          seq_padding: seqPadding,
          starting_number: startingOrNext,
          year_reset: yearReset,
          is_default: isDefault,
        });
        toast(`Created "${name}"`, "success");
      } else {
        await api.patch(`/api/compose/series/${existing!.id}?role=admin`, {
          name: name.trim(),
          scheme: scheme.trim(),
          prefix,
          seq_padding: seqPadding,
          next_number: startingOrNext,
          year_reset: yearReset,
          is_default: isDefault,
        });
        toast(`Updated "${name}"`, "success");
      }
      onSaved();
    } catch (e: any) {
      toast(`Save failed: ${e.message}`, "error");
    } finally {
      setBusy(false);
    }
  }

  const disabled =
    !name.trim() ||
    !scheme.includes("{seq}") ||
    (mode === "create" && !kind.trim());

  return (
    <div
      className="fixed inset-0 z-[1100] bg-black/60 backdrop-blur-sm flex items-center justify-center p-6"
      onClick={() => { if (!busy) onClose(); }}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            {mode === "create" ? <Plus className="w-4 h-4 text-rose-500" /> : <Edit3 className="w-4 h-4 text-rose-500" />}
            <span className="font-semibold">
              {mode === "create" ? "New series" : `Edit "${existing?.name}"`}
            </span>
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            className="w-7 h-7 rounded-md hover:bg-muted text-muted-foreground flex items-center justify-center disabled:opacity-50"
          >
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="p-5 space-y-3">
          {mode === "create" && (
            <Field label="Kind">
              <input
                autoFocus
                value={kind}
                onChange={e => setKind(e.target.value)}
                placeholder="rechnung · angebot · invoice · quote · faktura"
                className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
              />
              <div className="text-[10px] text-muted-foreground mt-1">
                The category. Templates whose args use names like <code>rechnungsnummer</code> /
                <code> invoice_number</code> will auto-pick the matching kind's default series.
              </div>
            </Field>
          )}

          <Field label="Display name">
            <input
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="Rechnungen"
              className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
            />
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Prefix">
              <input
                value={prefix}
                onChange={e => setPrefix(e.target.value)}
                placeholder="R-  or  empty"
                className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
              />
            </Field>
            <Field label="Sequence padding">
              <input
                type="number"
                min={1}
                max={8}
                value={seqPadding}
                onChange={e => setSeqPadding(Number(e.target.value))}
                className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
              />
            </Field>
          </div>

          <Field label="Scheme">
            <input
              value={scheme}
              onChange={e => setScheme(e.target.value)}
              placeholder="{year}-{seq}"
              className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm font-mono focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
            />
            <div className="text-[10px] text-muted-foreground mt-1">
              Placeholders: <code>{"{year}"}</code> · <code>{"{seq}"}</code> · <code>{"{prefix}"}</code>.
              Must contain <code>{"{seq}"}</code>.
            </div>
          </Field>

          <Field label={mode === "create"
            ? "Starting number (use this to migrate from an existing system — e.g. set to 48 if your last invoice was 47)"
            : "Next number"}>
            <input
              type="number"
              min={1}
              value={startingOrNext}
              onChange={e => setStartingOrNext(Number(e.target.value))}
              className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
            />
          </Field>

          <div className="bg-muted/30 border border-border rounded-md px-3 py-2 text-xs flex items-center gap-2">
            <span className="text-muted-foreground">Next number will look like:</span>
            <code className="font-mono text-foreground bg-card border border-border px-1.5 py-0.5 rounded">
              {previewExample}
            </code>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="flex items-center gap-2 text-xs cursor-pointer">
              <input
                type="checkbox"
                checked={yearReset}
                onChange={e => setYearReset(e.target.checked)}
                className="accent-rose-500"
              />
              <span>Reset each January</span>
            </label>
            <label className="flex items-center gap-2 text-xs cursor-pointer">
              <input
                type="checkbox"
                checked={isDefault}
                onChange={e => setIsDefault(e.target.checked)}
                className="accent-rose-500"
              />
              <span>Default for this kind</span>
            </label>
          </div>

          <div className="flex items-center justify-end gap-2 pt-3 border-t border-border">
            <button
              onClick={onClose}
              disabled={busy}
              className="px-3 py-1.5 text-xs rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              onClick={save}
              disabled={busy || disabled}
              className={cn(
                "px-3 py-1.5 text-xs rounded-md font-medium transition inline-flex items-center gap-1.5",
                !busy && !disabled
                  ? "bg-rose-500 hover:bg-rose-600 text-white"
                  : "bg-muted text-muted-foreground cursor-not-allowed",
              )}
            >
              {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : (mode === "create" ? "Create" : "Save")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ───────────────────────── Allocations (audit trail) ────────────────────

function AllocationsView({
  series, onBack, toast,
}: {
  series: DocumentSeries;
  onBack: () => void;
  toast: Toast;
}) {
  const [allocs, setAllocs] = useState<SeriesAllocation[] | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api.get<SeriesAllocation[]>(`/api/compose/series/${series.id}/allocations?role=admin`)
      .then(d => { if (alive) setAllocs(d); })
      .catch(e => { if (alive) toast(`Could not load allocations: ${e.message}`, "error"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [series.id, toast]);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 mb-2 text-sm">
        <button
          onClick={onBack}
          className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
        >
          <ChevronRight className="w-3 h-3 rotate-180" /> Back
        </button>
        <div className="flex items-center gap-2 ml-auto">
          <Hash className="w-3.5 h-3.5 text-rose-500" />
          <span className="font-semibold">{series.name}</span>
          <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded-full bg-muted/60 text-foreground/70">
            {series.kind}
          </span>
        </div>
      </div>

      <div className="text-[11px] text-muted-foreground bg-muted/30 border border-border rounded-md px-3 py-2 leading-relaxed">
        Audit trail of every number consumed from this series. Each row links to the
        Paperless document it was used for (when applicable), with a PDF SHA-256
        hash for Steuerprüfungs proof.
      </div>

      {loading && (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          <Loader2 className="w-4 h-4 animate-spin" />
        </div>
      )}
      {!loading && (allocs?.length || 0) === 0 && (
        <div className="text-center py-10 text-muted-foreground text-sm">
          <CheckCircle2 className="w-8 h-8 mx-auto mb-2 opacity-30" />
          No numbers consumed yet. They'll appear here after you Save to Paperless or Send.
        </div>
      )}
      {(allocs || []).map(a => (
        <div key={a.id} className="bg-card border border-border rounded-xl p-3 flex items-start gap-3">
          <div className="w-10 h-10 rounded-md bg-rose-500/10 flex items-center justify-center shrink-0">
            <FileText className="w-4 h-4 text-rose-500" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-baseline gap-2">
              <code className="font-mono text-sm text-foreground">{a.formatted}</code>
              <span className="text-[11px] text-muted-foreground">{formatDate(a.consumed_at)}</span>
            </div>
            {a.title && (
              <div className="text-sm text-foreground/80 truncate mt-0.5">{a.title}</div>
            )}
            <div className="flex items-center gap-2 mt-1">
              {a.paperless_doc_id && (
                <a
                  href={`/paperless/documents/${a.paperless_doc_id}/`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-[10px] inline-flex items-center gap-1 text-rose-500 hover:underline"
                >
                  <ExternalLink className="w-2.5 h-2.5" /> Paperless #{a.paperless_doc_id}
                </a>
              )}
              {a.pdf_sha256 && (
                <span className="text-[10px] text-muted-foreground font-mono" title={a.pdf_sha256}>
                  sha256:{a.pdf_sha256.slice(0, 12)}…
                </span>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ───────────────────────── Helpers ──────────────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-[11px] text-muted-foreground mb-1">{label}</div>
      {children}
    </label>
  );
}

function formatDate(iso: string): string {
  const d = new Date(iso.replace(" ", "T") + (iso.includes("T") ? "" : "Z"));
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" }) +
         " · " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
