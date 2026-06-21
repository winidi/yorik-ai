/* Suggestion engine panel — shared across modalities (email + wa).
 *
 * Renders pending typed suggestions for one source message:
 *   - draft_reply           → Accept inserts the body into email_drafts
 *                              OR wa_drafts depending on sourceKind;
 *                              the existing per-modality DraftPanel
 *                              picks it up via its normal pending-draft
 *                              query.
 *   - propose_meeting_slot  → Accept inserts into events; the calendar
 *                              UI sees it on next refresh.
 *
 * Polls every 10s while the message is open so a backend run that
 * completes after the user opens the message still surfaces. Empty
 * silence (no suggestions) is intentional UX — the engine should be
 * quiet when there's nothing useful to say.
 */

import { useState } from "react";

import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";

type Suggestion = {
  id: number;
  type: string;
  payload: Record<string, any>;
  confidence: "low" | "medium" | "high";
  reason: string;
  status: string;
  resolved: Record<string, any> | null;
  created_at: string;
  evidence: Array<{ id: number; kind: string; ref_id: number | null; snippet: string }>;
};

const CONFIDENCE_TINT: Record<string, string> = {
  high:   "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 ring-1 ring-emerald-500/20",
  medium: "bg-sky-500/10 text-sky-700 dark:text-sky-300 ring-1 ring-sky-500/20",
  low:    "bg-amber-500/10 text-amber-700 dark:text-amber-300 ring-1 ring-amber-500/20",
};

const TYPE_LABEL: Record<string, string> = {
  draft_reply:          "Reply",
  propose_meeting_slot: "Meeting",
};

export function SuggestionPanel({
  sourceKind = "email",
  sourceId,
  onAfterAccept,
}: {
  sourceKind?: "email" | "wa";
  sourceId: number;
  onAfterAccept?: () => void;
}) {
  const listApi = useApi<{ items: Suggestion[] }>(
    `/api/suggestions?source_kind=${sourceKind}&source_id=${sourceId}`,
    [sourceKind, sourceId],
    10000,
  );
  const items = listApi.data?.items || [];

  if (!items.length) return null;

  return (
    <div className="mt-4 space-y-2">
      <div className="text-xs uppercase tracking-wider text-muted-foreground font-medium">
        Yorik suggests
      </div>
      {items.map((s) => (
        <SuggestionCard
          key={s.id}
          suggestion={s}
          onAfterAction={() => {
            listApi.refetch?.();
            onAfterAccept?.();
          }}
        />
      ))}
    </div>
  );
}

function SuggestionCard({
  suggestion,
  onAfterAction,
}: {
  suggestion: Suggestion;
  onAfterAction: () => void;
}) {
  const [busy, setBusy] = useState<"" | "accept" | "dismiss">("");
  const [error, setError] = useState<string | null>(null);
  const [edited, setEdited] = useState<Record<string, any> | null>(null);
  const [editing, setEditing] = useState(false);

  const tint = CONFIDENCE_TINT[suggestion.confidence] || CONFIDENCE_TINT.medium;
  const label = TYPE_LABEL[suggestion.type] || suggestion.type;
  const payload = edited ?? suggestion.payload;

  async function accept() {
    setBusy("accept");
    setError(null);
    try {
      const body = edited ? { payload: edited } : {};
      await api.post(`/api/suggestions/${suggestion.id}/accept`, body);
      onAfterAction();
    } catch (e: any) {
      setError(e?.message || "Accept failed");
    } finally {
      setBusy("");
    }
  }

  async function dismiss() {
    setBusy("dismiss");
    setError(null);
    try {
      await api.post(`/api/suggestions/${suggestion.id}/dismiss`, {});
      onAfterAction();
    } catch (e: any) {
      setError(e?.message || "Dismiss failed");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card/40 p-3 space-y-2">
      <div className="flex items-center gap-2">
        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${tint}`}>
          {label}
        </span>
        <span className="text-xs text-muted-foreground capitalize">
          {suggestion.confidence}
        </span>
        {suggestion.reason && (
          <span className="text-xs text-muted-foreground truncate flex-1" title={suggestion.reason}>
            — {suggestion.reason}
          </span>
        )}
      </div>

      {suggestion.type === "draft_reply" && (
        <DraftReplyPreview
          payload={payload}
          editing={editing}
          onEdit={(body) => setEdited({ ...payload, body })}
        />
      )}
      {suggestion.type === "propose_meeting_slot" && (
        <MeetingSlotPreview
          payload={payload}
          editing={editing}
          onEdit={(fields) => setEdited({ ...payload, ...fields })}
        />
      )}

      {suggestion.evidence?.length > 0 && (
        <div className="text-[11px] text-muted-foreground flex flex-wrap gap-1">
          {suggestion.evidence.map((e) => (
            <span key={e.id} className="px-1.5 py-0.5 bg-muted/40 rounded" title={e.snippet}>
              {e.kind}{e.ref_id ? `#${e.ref_id}` : ""}
            </span>
          ))}
        </div>
      )}

      {error && (
        <div className="text-xs text-red-600 dark:text-red-400">{error}</div>
      )}

      <div className="flex items-center gap-2 pt-1">
        <button
          onClick={accept}
          disabled={!!busy}
          className="text-xs px-3 py-1 rounded bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-50"
        >
          {busy === "accept" ? "Accepting…" : edited ? "Accept edited" : "Accept"}
        </button>
        <button
          onClick={() => setEditing((v) => !v)}
          disabled={!!busy}
          className="text-xs px-3 py-1 rounded bg-muted hover:bg-muted/70 text-foreground disabled:opacity-50"
        >
          {editing ? "Done editing" : "Edit"}
        </button>
        <button
          onClick={dismiss}
          disabled={!!busy}
          className="text-xs px-3 py-1 rounded text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          {busy === "dismiss" ? "…" : "Dismiss"}
        </button>
      </div>
    </div>
  );
}

function DraftReplyPreview({
  payload,
  editing,
  onEdit,
}: {
  payload: Record<string, any>;
  editing: boolean;
  onEdit: (body: string) => void;
}) {
  const body = (payload?.body as string) || "";
  if (editing) {
    return (
      <textarea
        value={body}
        onChange={(e) => onEdit(e.target.value)}
        rows={8}
        className="w-full text-sm rounded border border-border bg-background p-2 font-sans whitespace-pre-wrap"
      />
    );
  }
  return (
    <div className="text-sm whitespace-pre-wrap leading-relaxed bg-muted/30 rounded p-2 max-h-48 overflow-y-auto">
      {body || "(empty draft)"}
    </div>
  );
}

function MeetingSlotPreview({
  payload,
  editing,
  onEdit,
}: {
  payload: Record<string, any>;
  editing: boolean;
  onEdit: (fields: Record<string, any>) => void;
}) {
  const title = (payload?.title as string) || "";
  const start = (payload?.starts_at as string) || "";
  const end = (payload?.ends_at as string) || "";
  const location = (payload?.location as string) || "";
  if (editing) {
    return (
      <div className="space-y-1 text-sm">
        <input
          value={title}
          onChange={(e) => onEdit({ title: e.target.value })}
          placeholder="Title"
          className="w-full px-2 py-1 rounded border border-border bg-background"
        />
        <div className="flex gap-2">
          <input
            value={start}
            onChange={(e) => onEdit({ starts_at: e.target.value })}
            placeholder="2026-06-25T14:00"
            className="flex-1 px-2 py-1 rounded border border-border bg-background"
          />
          <input
            value={end}
            onChange={(e) => onEdit({ ends_at: e.target.value })}
            placeholder="2026-06-25T15:00"
            className="flex-1 px-2 py-1 rounded border border-border bg-background"
          />
        </div>
        <input
          value={location}
          onChange={(e) => onEdit({ location: e.target.value })}
          placeholder="Location (optional)"
          className="w-full px-2 py-1 rounded border border-border bg-background"
        />
      </div>
    );
  }
  return (
    <div className="text-sm">
      <div className="font-medium">{title || "(untitled)"}</div>
      <div className="text-muted-foreground">
        {start} → {end}
        {location && <span> · {location}</span>}
      </div>
    </div>
  );
}
