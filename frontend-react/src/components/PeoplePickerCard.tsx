/**
 * People picker — face-naming card shown in chat when find_photo can't
 * resolve a person name the user mentioned (e.g. "Foto von Sara" but
 * Immich has no face cluster labelled "Sara" yet).
 *
 * The skill emits one card listing the missing name(s) plus the top
 * N unnamed face clusters from Immich (ordered by face count, so the
 * most-photographed unknown face shows first — usually a close
 * family member). The user picks "this is Sara" → we PUT the label
 * to Immich → resume_message fires the original find_photo again,
 * this time with Sara resolving cleanly.
 *
 * Multi-name case ("photos of me and Sara"): user can assign each
 * missing name to a separate face. "Done" sends the resume regardless
 * of how many got labelled — partial labelling is fine, the unlabeled
 * ones just won't match in the retry.
 */

import { useState } from "react";
import { Loader2, Check, UserPlus, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";

export interface PeopleCandidate {
  id:             string;
  thumbnail_url:  string;
  face_count:     number;
  /** When set, this face is already labeled in Immich. Tapping it
   *  re-runs the search with this existing name instead of renaming
   *  the cluster — covers the "I typed 'Sara' but she's labeled
   *  'Sarah'" case. */
  name?:          string | null;
}

export interface PeoplePickerAction {
  type:           "people_picker";
  missing_names:  string[];
  candidates:     PeopleCandidate[];
  resume_skill?:  string;
  resume_args?:   Record<string, unknown>;
}

interface Props {
  action:   PeoplePickerAction;
  onSubmit: (resumeMessage: string) => void;
}

export function PeoplePickerCard({ action, onSubmit }: Props) {
  // candidateId → assigned name (one of action.missing_names) or null
  const [assignments, setAssignments] = useState<Record<string, string | null>>({});
  const [labeling, setLabeling] = useState<string | null>(null);
  const [labeledIds, setLabeledIds] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);
  // Per missing-name, has anyone been assigned to it?
  const assignedNames = new Set(Object.values(assignments).filter(Boolean) as string[]);

  async function assignAndLabel(candidateId: string, name: string) {
    setLabeling(candidateId);
    setErrors(e => ({ ...e, [candidateId]: "" }));
    try {
      await api.post(`/api/immich/people/${encodeURIComponent(candidateId)}/name`, { name });
      setAssignments(a => ({ ...a, [candidateId]: name }));
      setLabeledIds(s => new Set(s).add(candidateId));
    } catch (e: any) {
      setErrors(err => ({ ...err, [candidateId]: e?.message || "Label failed" }));
    } finally {
      setLabeling(null);
    }
  }

  /** "Use the existing label" path: face is already named in Immich.
   *  We don't relabel — we resume the search with that name instead
   *  of the (mistyped/wrong-spelled) missing_name. */
  function pickExistingLabel(candidateId: string, existingName: string) {
    if (submitted) return;
    setSubmitted(true);
    const skill = action.resume_skill || "find_photo";
    // Resume args carry the original search params; swap in the
    // existing label as the person/people term so the next find_photo
    // call hits the right cluster.
    const args = { ...(action.resume_args || {}) } as Record<string, unknown>;
    if (action.missing_names.length === 1) {
      args.person = existingName;
      delete args.people;
    } else {
      // Multi-name: replace the missing name the user tied this face
      // to. If we don't know which one they meant, replace the first.
      const assignedTo = assignments[candidateId] || action.missing_names[0];
      const currentPeople = String(args.people || action.missing_names.join(", "));
      args.people = currentPeople
        .split(",").map(s => s.trim())
        .map(n => n === assignedTo ? existingName : n)
        .filter(Boolean).join(", ");
      delete args.person;
    }
    const argsJson = JSON.stringify(args);
    onSubmit(
      `[people_labeled] Existing label "${existingName}" matched the face — ` +
      `using it instead of the original search term. ` +
      `Re-run skill=${skill} with args=${argsJson}.`
    );
  }

  function done(skipped: boolean = false) {
    if (submitted) return;
    setSubmitted(true);
    const labeled = Object.entries(assignments)
      .filter(([_, n]) => !!n)
      .map(([id, n]) => `${n} → ${id}`);
    const parts: string[] = [];
    if (labeled.length > 0) {
      parts.push(`Labeled ${labeled.length} face(s) in Immich: ${labeled.join("; ")}.`);
    }
    if (skipped) {
      parts.push("User skipped labeling the remaining face(s) — proceed without them.");
    }
    // Resume message format the agent loop recognises (same shape as
    // [photo_picked] / [form_submit]). The LLM sees this and re-runs
    // find_photo with the original args; the freshly-labeled faces
    // will resolve cleanly now.
    const skill = action.resume_skill || "find_photo";
    const argsJson = JSON.stringify(action.resume_args || {});
    onSubmit(
      `[people_labeled] ${parts.join(" ")} ` +
      `Re-run skill=${skill} with args=${argsJson}.`
    );
  }

  if (submitted) {
    return (
      <div className="mt-2 border border-emerald-500/30 bg-emerald-500/10 rounded-xl p-3 text-xs text-emerald-700 dark:text-emerald-300 inline-flex items-center gap-2 max-w-md">
        <Check className="w-4 h-4 shrink-0" />
        Re-running photo search with the freshly labeled faces…
      </div>
    );
  }

  return (
    <div className="mt-2 border border-border rounded-xl bg-card/80 max-w-md overflow-hidden">
      <div className="px-3 pt-2.5 pb-2 border-b border-border/60">
        <div className="flex items-center gap-1.5 mb-1">
          <UserPlus className="w-3.5 h-3.5 text-violet-500" />
          <span className="text-xs font-semibold">Identify the face(s)</span>
        </div>
        <div className="text-[11px] text-muted-foreground leading-relaxed">
          {action.missing_names.length === 1 ? (
            <>Immich doesn't have a face labeled <strong className="text-foreground">{action.missing_names[0]}</strong> yet — tap the right thumbnail below.</>
          ) : (
            <>Immich doesn't have these labeled yet: <strong className="text-foreground">{action.missing_names.join(", ")}</strong>. Tap a thumbnail and pick the name.</>
          )}
          {action.candidates.some(c => c.name) && (
            <span className="block mt-1 text-[10px]">
              <span className="inline-block w-2 h-2 rounded bg-amber-500/80 align-middle mr-1" />
              Amber tiles are already labeled — tap to use that label instead.
            </span>
          )}
        </div>
      </div>

      <div className="p-2 grid grid-cols-4 gap-1.5 max-h-[50vh] overflow-y-auto">
        {action.candidates.map(c => {
          const assigned = assignments[c.id] || null;
          const isLabeled = labeledIds.has(c.id);
          const isLabelingThis = labeling === c.id;
          const hasExistingName = !!(c.name && c.name.trim());
          return (
            <div
              key={c.id}
              className={cn(
                "relative aspect-square rounded-md overflow-hidden border-2 transition",
                isLabeled
                  ? "border-emerald-500/60 ring-2 ring-emerald-500/30"
                  : hasExistingName
                  ? "border-amber-500/50 hover:border-amber-500"
                  : "border-border hover:border-violet-500/50",
                isLabelingThis && "opacity-60",
              )}
            >
              <img
                src={c.thumbnail_url}
                alt=""
                loading="lazy"
                className="w-full h-full object-cover"
                onError={(e) => { (e.currentTarget as HTMLImageElement).style.opacity = "0.3"; }}
              />
              {/* Face count badge */}
              <div className="absolute top-0.5 left-0.5 text-[9px] bg-black/60 text-white px-1 py-0.5 rounded tabular-nums">
                {c.face_count}×
              </div>
              {/* Already-named: show the existing label in an amber badge so
                  the user can spot "that's actually labeled as Sarah", and a
                  one-tap CTA to retry the search with that name (no rename). */}
              {hasExistingName && !isLabeled && (
                <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 to-transparent p-1 pt-3">
                  <button
                    onClick={() => pickExistingLabel(c.id, c.name!)}
                    disabled={!!labeling || submitted}
                    className="w-full text-[10px] py-0.5 px-1.5 rounded bg-amber-500/90 text-white font-medium hover:bg-amber-500 transition truncate"
                    title={`Use existing label "${c.name}" for this search`}
                  >
                    = {c.name}
                  </button>
                </div>
              )}
              {/* Label-as overlay (dropdown of missing names) — unnamed only */}
              {!isLabeled && !hasExistingName && (
                <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 to-transparent p-1 pt-3">
                  {action.missing_names.length === 1 ? (
                    <button
                      onClick={() => assignAndLabel(c.id, action.missing_names[0])}
                      disabled={!!labeling}
                      className="w-full text-[10px] py-0.5 px-1.5 rounded bg-violet-500/90 text-white font-medium hover:bg-violet-500 transition truncate"
                    >
                      {isLabelingThis ? <Loader2 className="w-3 h-3 animate-spin inline" /> : `= ${action.missing_names[0]}`}
                    </button>
                  ) : (
                    <select
                      value=""
                      onChange={e => e.target.value && assignAndLabel(c.id, e.target.value)}
                      disabled={!!labeling}
                      className="w-full text-[10px] py-0.5 px-1 rounded bg-violet-500/90 text-white font-medium border-0 cursor-pointer truncate"
                    >
                      <option value="">Label…</option>
                      {action.missing_names.filter(n => !assignedNames.has(n)).map(n => (
                        <option key={n} value={n}>{n}</option>
                      ))}
                    </select>
                  )}
                </div>
              )}
              {isLabeled && (
                <div className="absolute inset-x-0 bottom-0 bg-emerald-500/95 text-white text-[10px] font-medium px-1 py-0.5 text-center truncate">
                  <Check className="w-2.5 h-2.5 inline mr-0.5" />{assigned}
                </div>
              )}
              {errors[c.id] && (
                <div className="absolute inset-0 bg-red-500/80 text-white text-[9px] p-1 flex items-center justify-center text-center">
                  {errors[c.id]}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="px-3 py-2 border-t border-border/60 bg-muted/30 flex items-center gap-2">
        <span className="text-[10px] text-muted-foreground flex-1">
          {labeledIds.size > 0
            ? `${labeledIds.size} labeled · click Done to re-run search`
            : "Pick a face above, or skip if none match"}
        </span>
        <button
          onClick={() => done(true)}
          className="text-[11px] px-2 py-1 rounded text-muted-foreground hover:text-foreground hover:bg-muted inline-flex items-center gap-1"
        >
          <X className="w-3 h-3" /> Skip
        </button>
        <button
          onClick={() => done(false)}
          disabled={labeledIds.size === 0}
          className={cn(
            "text-[11px] px-3 py-1 rounded font-medium inline-flex items-center gap-1",
            labeledIds.size === 0
              ? "bg-muted text-muted-foreground cursor-not-allowed"
              : "bg-violet-500 hover:bg-violet-600 text-white",
          )}
        >
          <Check className="w-3 h-3" /> Done
        </button>
      </div>
    </div>
  );
}
