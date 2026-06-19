/**
 * Visual photo picker shown inline in ComposeAgentChat when the LLM
 * emits a `photo_picker` ui_action (from the propose_inline_photo
 * skill). Thumbnail grid with one-click select + optional caption.
 *
 * On submit we synthesize a [photo_picked] resume message that hands
 * the picked photo's URL back to the LLM playbook, which proceeds
 * with compose_draft(args={inline_image_url, inline_image_caption}).
 */

import { useState } from "react";
import { Image as ImageIcon, Sparkles, Check, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

export interface PhotoCandidate {
  id: string;
  thumbnail_url: string;
  embed_url: string;
  original_name?: string;
  taken_at?: string;
  type?: string;
}

export interface PhotoPickerAction {
  type: "photo_picker";
  source_skill?: string;
  title: string;
  context: string;
  candidates: PhotoCandidate[];
  next_playbook_step?: string;
  resume_skill?: string;
  resume_args?: Record<string, unknown>;
}

interface Props {
  action: PhotoPickerAction;
  onSubmit: (resumeMessage: string) => void;
}

export function PhotoPickerCard({ action, onSubmit }: Props) {
  const [pickedId, setPickedId] = useState<string | null>(null);
  const [caption, setCaption] = useState<string>("");
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);

  const picked = action.candidates.find(c => c.id === pickedId) || null;

  function handleSubmit() {
    if (!picked || submitted) return;
    setBusy(true);

    // Build resume message that splats into the next skill call.
    // The LLM playbook will call compose_draft with args={inline_image_url, ...}
    // and existing_draft_id (when present in resume_args).
    const filledArgs: Record<string, unknown> = {
      inline_image_url: picked.embed_url,
    };
    if (caption.trim()) {
      filledArgs.inline_image_caption = caption.trim();
    }

    const resumeKwargs = {
      ...(action.resume_args || {}),
      args: filledArgs,
    };
    const argsRepr = Object.entries(resumeKwargs)
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join(", ");
    const nextStep = action.next_playbook_step || action.resume_skill || "compose_draft";

    const msg = (
      `[photo_picked from=${action.source_skill || "photo_picker"}] ` +
      `picked photo id=${picked.id}` +
      (picked.taken_at ? ` (taken ${picked.taken_at.slice(0, 10)})` : "") +
      `, caption=${JSON.stringify(caption.trim() || null)}. ` +
      `Next playbook step: call ${nextStep}(${argsRepr}). ` +
      `Pass these args to compose_draft so the photo lands in the letter. ` +
      `Do NOT re-show the picker.`
    );

    setSubmitted(true);
    setBusy(false);
    onSubmit(msg);
  }

  if (submitted) {
    return (
      <div className="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm">
        <div className="flex items-center gap-2 text-emerald-700 dark:text-emerald-400">
          <Check className="w-4 h-4" /> Photo applied — Yorik is inserting it now.
        </div>
      </div>
    );
  }

  if (action.candidates.length === 0) {
    return (
      <div className="rounded-2xl border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-muted-foreground">
        No matching photos found. Tip: upload one directly via the
        📷 icon in the toolbar.
      </div>
    );
  }

  return (
    <div className="rounded-2xl border border-violet-500/30 bg-violet-500/5 px-4 py-3 text-sm space-y-3">
      <div className="flex items-start gap-2">
        <Sparkles className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="font-semibold text-foreground">{action.title}</div>
          <div className="text-xs text-muted-foreground mt-0.5">{action.context}</div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2">
        {action.candidates.map(c => {
          const isPicked = pickedId === c.id;
          const date = c.taken_at ? c.taken_at.slice(0, 10) : "";
          return (
            <button
              key={c.id}
              type="button"
              onClick={() => setPickedId(c.id)}
              className={cn(
                "relative aspect-square rounded-lg overflow-hidden border-2 transition group",
                isPicked
                  ? "border-violet-500 ring-2 ring-violet-500/30 ring-offset-1 ring-offset-background"
                  : "border-transparent hover:border-violet-500/40",
              )}
              title={[c.original_name, date].filter(Boolean).join(" · ")}
            >
              {c.thumbnail_url ? (
                <img
                  src={c.thumbnail_url}
                  alt={c.original_name || ""}
                  loading="lazy"
                  className="w-full h-full object-cover transition group-hover:scale-105"
                />
              ) : (
                <div className="w-full h-full flex items-center justify-center bg-muted">
                  <ImageIcon className="w-6 h-6 text-muted-foreground" />
                </div>
              )}
              {date && (
                <div className="absolute bottom-0 inset-x-0 bg-gradient-to-t from-black/70 to-transparent text-[11px] md:text-[10px] text-white px-1.5 py-1 text-right">
                  {date}
                </div>
              )}
              {isPicked && (
                <div className="absolute top-1 right-1 w-5 h-5 rounded-full bg-violet-500 text-white flex items-center justify-center shadow">
                  <Check className="w-3 h-3" />
                </div>
              )}
            </button>
          );
        })}
      </div>

      <div>
        <label className="block text-[11px] text-muted-foreground mb-1">
          Caption (optional)
        </label>
        <input
          type="text"
          value={caption}
          onChange={e => setCaption(e.target.value)}
          disabled={!picked}
          placeholder={picked ? 'e.g. "Sicily, August 2024"' : "Pick a photo first"}
          className="w-full h-11 md:h-8 px-2 rounded-md bg-background border border-border text-sm focus:outline-none focus:ring-1 focus:ring-ring/40 disabled:opacity-50"
        />
      </div>

      <div className="flex items-center gap-2 pt-1">
        <button
          type="button"
          onClick={handleSubmit}
          disabled={busy || !picked}
          className="h-8 px-3 rounded-md bg-violet-500 text-white text-xs font-medium hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5"
        >
          {busy
            ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
            : <Check className="w-3.5 h-3.5" />}
          Use this photo
        </button>
        {!picked && (
          <span className="text-[11px] text-muted-foreground">
            Click a photo.
          </span>
        )}
      </div>
    </div>
  );
}
