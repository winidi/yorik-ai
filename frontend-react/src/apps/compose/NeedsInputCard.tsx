/**
 * Inline form card rendered inside ComposeAgentChat when a skill emits
 * a `needs_input` ui_action. Lets the user fill the missing fields
 * directly (with optional suggestions pre-mined from Paperless) instead
 * of typing the answer in prose.
 *
 * On submit:
 *   1. If `save_to_contact` is present AND the checkbox is checked,
 *      POST /api/skills/add_contact_address/invoke directly so the
 *      address sticks for next time.
 *   2. Hand a synthesised confirmation message back to the parent
 *      ComposeAgentChat so it can POST /api/ask and let the LLM
 *      playbook resume (compose_check_recipient → compose_draft).
 */

import { useState } from "react";
import {
  Loader2, Sparkles, FileText, Save, ClipboardPaste, ChevronDown,
  ChevronUp, RotateCcw,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api, ApiError } from "@/lib/api";

export interface NeedsInputField {
  key: string;
  label: string;
  value?: string;
  required?: boolean;
  pattern?: string;
  /** "textarea" renders a multi-line input (body_text etc.); default text. */
  input?: "text" | "textarea";
  /** Helper text shown below the field. */
  hint?: string;
  /** Template role from ask_user_for_args. role in {"body","freeform_text"}
   *  (or explicit from_intent) flips this field to "intent-derived", which
   *  surfaces the "Yorik formuliert für mich" sparkle button so the user
   *  can describe what they want in freetalk and have the LLM polish it
   *  into a template-respecting body. */
  role?: string;
  from_intent?: boolean;
}

export interface NeedsInputSuggestion {
  label: string;
  values: Record<string, string>;
  source_doc_id?: number;
  source_doc_title?: string;
  confidence?: number;
}

export interface NeedsInputAction {
  type: "needs_input";
  title: string;
  context: string;
  fields: NeedsInputField[];
  suggestions?: NeedsInputSuggestion[];
  save_to_contact?: {
    contact_id: number;
    kind: string;
    default_checked: boolean;
    label: string;
  };
  /** Which skill emitted this form — surfaces in the resume_message so
   *  the LLM knows the call provenance and can route correctly. */
  source_skill?: string;
  /** Which skill the LLM should call AFTER the user submits. Distinct
   *  from resume_skill because the LLM might want to chain another
   *  check before the final draft (e.g. address-form → check template
   *  args → draft). */
  next_playbook_step?: string;
  resume_skill?: string;
  resume_args?: Record<string, unknown>;
}

interface Props {
  action: NeedsInputAction;
  /** Called with a synthesised user message after the form is saved.
   *  Parent re-posts to /api/ask so the LLM resumes the playbook. */
  onSubmit: (resumeMessage: string) => void;
  /** Surfaces save-step errors to the chat as a toast. */
  toast: (text: string, kind?: "info" | "success" | "error") => void;
}

export function NeedsInputCard({ action, onSubmit, toast }: Props) {
  const [values, setValues] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const f of action.fields) init[f.key] = f.value || "";
    return init;
  });
  const [saveChecked, setSaveChecked] = useState(
    action.save_to_contact?.default_checked ?? true,
  );
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  // "Yorik formuliert für mich" — per-field inline panel. The user types
  // freetalk ("schöne grüße aus paris, wetter super, kommen sonntag
  // zurück"), Yorik polishes it into a template-respecting body_text
  // (and may suggest a Betreff when the field is empty). State is keyed
  // by field key so multiple intent-derived fields stay independent.
  const [polishOpen, setPolishOpen]   = useState<Record<string, boolean>>({});
  const [polishIntent, setPolishIntent] = useState<Record<string, string>>({});
  const [polishBusy, setPolishBusy]   = useState<Record<string, boolean>>({});
  const [polishError, setPolishError] = useState<Record<string, string>>({});

  // Pull template_id / contact_id from the resume_args the LLM/skill
  // attached — same source the form-submit hint already uses.
  const templateId =
    (action.resume_args as any)?.template_id as string | undefined;
  const contactId =
    (action.resume_args as any)?.contact_id as number | undefined;

  // Intent-derived: role flagged on the template, or explicit from_intent.
  const isIntentField = (f: NeedsInputField): boolean =>
    f.role === "body" || f.role === "freeform_text" || f.from_intent === true;

  async function runPolish(fieldKey: string) {
    const intent = (polishIntent[fieldKey] || "").trim()
      || (values[fieldKey] || "").trim();
    if (!intent || !templateId) return;
    setPolishBusy(s => ({ ...s, [fieldKey]: true }));
    setPolishError(s => ({ ...s, [fieldKey]: "" }));
    try {
      const r = await api.post<{ body_text: string; betreff?: string | null }>(
        "/api/compose/polish",
        {
          intent,
          template_id: templateId,
          contact_id: contactId,
          field_key: fieldKey,
          // Only suggest a Betreff when the form has one AND it's empty
          // right now — never clobber what the user typed.
          suggest_betreff: action.fields.some(x => x.key === "betreff")
            && !(values.betreff || "").trim(),
        },
      );
      if (r?.body_text) {
        setValues(prev => {
          const next = { ...prev, [fieldKey]: r.body_text };
          if (r.betreff && !(prev.betreff || "").trim()) next.betreff = r.betreff;
          return next;
        });
        setPolishOpen(s => ({ ...s, [fieldKey]: false }));
        setPolishIntent(s => ({ ...s, [fieldKey]: "" }));
      } else {
        setPolishError(s => ({ ...s, [fieldKey]: "Yorik konnte nichts polieren — bitte umformulieren." }));
      }
    } catch (err: any) {
      const msg = err instanceof ApiError
        ? (err.message || "Polish-Fehler")
        : (err?.message || "Polish-Fehler");
      setPolishError(s => ({ ...s, [fieldKey]: msg }));
    } finally {
      setPolishBusy(s => ({ ...s, [fieldKey]: false }));
    }
  }
  // "Aus Text füllen" panel — collapsed until the user opens it. The
  // textarea + extraction is local state; on success we merge into
  // `values` honouring the empty-only-by-default policy.
  const [extractOpen, setExtractOpen] = useState(false);
  const [extractText, setExtractText] = useState("");
  const [extracting, setExtracting] = useState(false);
  const [extractError, setExtractError] = useState<string | null>(null);
  const [lastFilledKeys, setLastFilledKeys] = useState<string[]>([]);
  // True after an extraction has run at least once — the second click
  // becomes "↻ Überschreiben" (overwrite even filled cells).
  const [extractedOnce, setExtractedOnce] = useState(false);

  function applySuggestion(s: NeedsInputSuggestion) {
    setValues(v => ({ ...v, ...s.values }));
  }

  async function runExtract(overwrite: boolean) {
    if (!extractText.trim() || extracting) return;
    setExtracting(true);
    setExtractError(null);
    try {
      const r = await api.post<{ values: Record<string, string> }>(
        "/api/compose/extract-fields",
        {
          text: extractText,
          fields: action.fields.map(f => ({
            key: f.key,
            label: f.label,
            pattern: f.pattern,
          })),
        },
      );
      const incoming = r.values || {};
      // Empty-only fill unless overwrite=true. Either way, only honour
      // keys that exist in the form schema (server already filters,
      // but we double-check on the client).
      const filledThisRun: string[] = [];
      setValues(prev => {
        const next = { ...prev };
        for (const f of action.fields) {
          const incomingVal = incoming[f.key];
          if (!incomingVal) continue;
          const cur = (next[f.key] || "").trim();
          if (cur && !overwrite) continue;
          next[f.key] = incomingVal;
          filledThisRun.push(f.key);
        }
        return next;
      });
      setLastFilledKeys(filledThisRun);
      setExtractedOnce(true);
      // Auto-close if we filled at least one cell — the form's the
      // focus now. Stay open if nothing was extracted so the user
      // sees the "nothing found" hint.
      if (filledThisRun.length > 0) setExtractOpen(false);
    } catch (err: any) {
      const msg = err instanceof ApiError ? err.message : String(err);
      setExtractError(`Konnte den Text nicht auswerten: ${msg}`);
    } finally {
      setExtracting(false);
    }
  }

  function fieldValid(f: NeedsInputField, v: string): boolean {
    if (f.required && !v.trim()) return false;
    if (f.pattern && v.trim()) {
      try { return new RegExp(f.pattern).test(v.trim()); } catch { return true; }
    }
    return true;
  }

  const allValid = action.fields.every(f => fieldValid(f, values[f.key] || ""));

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || submitted || !allValid) return;
    setBusy(true);

    // 1) Save to contact (if requested + checked) — direct API call so
    //    the address is on file even if the LLM round-trip fails.
    if (action.save_to_contact && saveChecked) {
      const stc = action.save_to_contact;
      try {
        await api.post(`/api/skills/add_contact_address/invoke`, {
          contact_id: stc.contact_id,
          kind:       stc.kind,
          line1:      values.line1 || "",
          line2:      values.line2 || "",
          postcode:   values.postcode || "",
          city:       values.city || "",
          country:    values.country || "DE",
        });
      } catch (err: any) {
        // Don't block resume — surface and continue. User can re-save
        // later via the contacts UI.
        toast(`Konnte Adresse nicht speichern: ${err?.message || err}`, "error");
      }
    }

    // 2) Build the resume message for the LLM. Verbose on purpose:
    //    qwen3 follows explicit "call X next" instructions much more
    //    reliably than implicit cues. We label the source skill so the
    //    LLM doesn't conflate "address form done" with "all checks done".
    const filledEntries = Object.entries(values).filter(([, v]) => v && v.trim());
    const filledStr = filledEntries
      .map(([k, v]) => `${k}="${v.trim()}"`)
      .join(", ");
    // JSON-serialized args dict the LLM can splat directly into the
    // next skill's `args=` parameter (works for compose_draft +
    // compose_check_template_args alike).
    const filledArgsJson = JSON.stringify(
      Object.fromEntries(filledEntries.map(([k, v]) => [k, v.trim()])),
    );

    const source = action.source_skill || "needs_input form";
    const savedNote = action.save_to_contact && saveChecked
      ? " (already saved to the contact via add_contact_address)"
      : "";

    let resumeHint = "";
    if (action.next_playbook_step && action.resume_args) {
      const resumeKwargs = {
        ...action.resume_args,
        args: Object.fromEntries(filledEntries.map(([k, v]) => [k, v.trim()])),
      };
      const argsRepr = Object.entries(resumeKwargs)
        .map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
      resumeHint = ` Next playbook step: call ${action.next_playbook_step}(${argsRepr}).`;
    } else if (action.resume_skill && action.resume_args) {
      const argsRepr = Object.entries(action.resume_args)
        .map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
      resumeHint = ` Next: call ${action.resume_skill}(${argsRepr}, args=${filledArgsJson}).`;
    }

    const msg = (
      `[form_submit from=${source}] Fields the user filled: ${filledStr || "(all skipped)"}.${savedNote}` +
      `${resumeHint} Pass these filled values as the \`args\` dict to the next skill call. Do NOT re-show the same form.`
    );

    setSubmitted(true);
    setBusy(false);
    onSubmit(msg);
  }

  if (submitted) {
    return (
      <div className="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm">
        <div className="flex items-center gap-2 text-emerald-700 dark:text-emerald-400">
          <Save className="w-4 h-4" /> Data saved — Yorik is writing now.
        </div>
      </div>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="rounded-2xl border border-violet-500/30 bg-violet-500/5 px-4 py-3 text-sm space-y-3"
    >
      <div className="flex items-start gap-2">
        <Sparkles className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="font-semibold text-foreground">{action.title}</div>
          <div className="text-xs text-muted-foreground mt-0.5">{action.context}</div>
        </div>
      </div>

      {action.suggestions && action.suggestions.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {action.suggestions.map((s, i) => (
            <button
              key={i}
              type="button"
              onClick={() => applySuggestion(s)}
              className="inline-flex items-center gap-1.5 text-xs md:text-[11px] px-3 md:px-2.5 py-1.5 md:py-1 rounded-full border border-violet-500/30 bg-background hover:bg-violet-500/10 transition"
              title={Object.entries(s.values).map(([k, v]) => `${k}: ${v}`).join("\n")}
            >
              <FileText className="w-3 h-3" />
              {s.label}
              {typeof s.confidence === "number" && (
                <span className="text-muted-foreground">
                  {(s.confidence * 100).toFixed(0)}%
                </span>
              )}
            </button>
          ))}
        </div>
      )}

      {/* "Aus Text füllen" — paste an arbitrary blob, LLM maps it onto
          the form's named fields. Collapsed until clicked so the
          common case (just type the values) isn't cluttered. */}
      <div className="rounded-md border border-violet-500/15 bg-background/50">
        <button
          type="button"
          onClick={() => setExtractOpen(o => !o)}
          className="w-full px-2.5 py-1.5 flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground transition"
        >
          <ClipboardPaste className="w-3 h-3" />
          <span className="font-medium">Fill from text</span>
          <span className="text-muted-foreground/80">— paste an email or note, Yorik maps the fields</span>
          <span className="ml-auto">
            {extractOpen ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
          </span>
        </button>
        {extractOpen && (
          <div className="px-2.5 pb-2.5 space-y-2">
            <textarea
              value={extractText}
              onChange={(e) => setExtractText(e.target.value)}
              placeholder="e.g. an email or note with name, address, date, amount…"
              rows={4}
              className="w-full text-xs bg-background border border-border rounded-md px-2 py-1.5 resize-y focus:outline-none focus:ring-1 focus:ring-ring/40"
            />
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => runExtract(false)}
                disabled={extracting || !extractText.trim()}
                className="h-9 md:h-7 px-3 md:px-2.5 rounded-md bg-violet-500 text-white text-xs md:text-[11px] font-medium hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5"
              >
                {extracting
                  ? <Loader2 className="w-3 h-3 animate-spin" />
                  : <Sparkles className="w-3 h-3" />}
                Fill fields
              </button>
              {extractedOnce && (
                <button
                  type="button"
                  onClick={() => runExtract(true)}
                  disabled={extracting || !extractText.trim()}
                  className="h-7 px-2 rounded-md text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-50 flex items-center gap-1"
                  title="Also overwrite already-filled fields"
                >
                  <RotateCcw className="w-3 h-3" /> Overwrite
                </button>
              )}
              {lastFilledKeys.length > 0 && !extracting && (
                <span className="text-[10px] text-emerald-600 dark:text-emerald-400">
                  {lastFilledKeys.length} field{lastFilledKeys.length === 1 ? "" : "s"} filled
                </span>
              )}
              {extractedOnce && lastFilledKeys.length === 0 && !extracting && (
                <span className="text-[10px] text-muted-foreground">
                  Nichts Passendes gefunden.
                </span>
              )}
            </div>
            {extractError && (
              <div className="text-[10px] text-rose-500">{extractError}</div>
            )}
          </div>
        )}
      </div>

      <div className="space-y-1.5">
        {action.fields.map((f) => {
          const v = values[f.key] || "";
          const invalid = !fieldValid(f, v) && v !== "";
          const wasJustFilled = lastFilledKeys.includes(f.key);
          const isLong = f.input === "textarea";
          const onChange = (val: string) => {
            setValues(prev => ({ ...prev, [f.key]: val }));
            if (wasJustFilled) {
              setLastFilledKeys(keys => keys.filter(k => k !== f.key));
            }
          };
          const fieldClasses = cn(
            "flex-1 text-sm bg-background border rounded-md px-2 focus:outline-none focus:ring-1 focus:ring-ring/40 transition",
            isLong ? "py-1.5 min-h-[5.5rem] resize-y"
                   : "h-11 md:h-auto md:py-1",
            invalid ? "border-rose-500/60"
              : wasJustFilled
                ? "border-emerald-500/50 bg-emerald-500/[0.04]"
                : "border-border",
          );
          const isIntent = isIntentField(f) && !!templateId;
          const panelOpen = polishOpen[f.key] || false;
          const intentText = polishIntent[f.key] || "";
          const busyP = polishBusy[f.key] || false;
          const errP = polishError[f.key] || "";
          return (
            <div
              key={f.key}
              className={cn("gap-2", isLong ? "flex flex-col" : "flex items-center")}
            >
              <div className={cn(
                "flex items-center gap-2",
                isLong ? "w-full" : "w-32 shrink-0",
              )}>
                <label className="text-xs text-muted-foreground">
                  {f.label}{f.required && <span className="text-rose-500">*</span>}
                </label>
                {isIntent && isLong && (
                  <button
                    type="button"
                    onClick={() => setPolishOpen(s => ({ ...s, [f.key]: !panelOpen }))}
                    className="ml-auto inline-flex items-center gap-1 text-[10px] text-violet-500 hover:text-violet-600 transition"
                    title="Yorik formuliert für mich aus Stichworten"
                  >
                    <Sparkles className="w-3 h-3" />
                    <span>{panelOpen ? "Schließen" : "Yorik formuliert für mich"}</span>
                  </button>
                )}
              </div>
              {/* Inline polish panel — appears above the textarea when
                  the user clicks the sparkle. Local state per field so
                  multiple intent fields stay independent. */}
              {isIntent && panelOpen && (
                <div className="rounded-md border border-violet-500/30 bg-violet-500/[0.04] p-2 space-y-2">
                  <div className="text-[11px] text-muted-foreground">
                    Was möchtest du schreiben? Stichworte reichen — Yorik macht den Rest.
                  </div>
                  <textarea
                    value={intentText}
                    onChange={(e) => setPolishIntent(s => ({ ...s, [f.key]: e.target.value }))}
                    rows={3}
                    placeholder="z.B. 'schöne grüße aus paris, wetter top, kommen sonntag zurück'"
                    className="w-full text-xs bg-background border border-border rounded-md px-2 py-1.5 resize-y focus:outline-none focus:ring-1 focus:ring-ring/40"
                  />
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => runPolish(f.key)}
                      disabled={busyP || !(intentText.trim() || (values[f.key] || "").trim())}
                      className="h-7 px-2.5 rounded-md bg-violet-500 text-white text-[11px] font-medium hover:opacity-90 disabled:opacity-50 inline-flex items-center gap-1.5"
                    >
                      {busyP
                        ? <Loader2 className="w-3 h-3 animate-spin" />
                        : <Sparkles className="w-3 h-3" />}
                      Yorik formuliert
                    </button>
                    {!intentText.trim() && (values[f.key] || "").trim() && (
                      <span className="text-[10px] text-muted-foreground">
                        (verwendet was du schon getippt hast)
                      </span>
                    )}
                  </div>
                  {errP && (
                    <div className="text-[10px] text-rose-500">{errP}</div>
                  )}
                </div>
              )}
              {isLong ? (
                <textarea
                  value={v}
                  onChange={(e) => onChange(e.target.value)}
                  rows={4}
                  className={fieldClasses}
                  placeholder={f.hint || ""}
                />
              ) : (
                <input
                  type="text"
                  value={v}
                  onChange={(e) => onChange(e.target.value)}
                  className={fieldClasses}
                  placeholder={f.pattern ? `(${f.pattern})` : (f.hint || "")}
                />
              )}
              {isLong && f.hint && (
                <div className="text-[10px] text-muted-foreground/80 pl-0.5">{f.hint}</div>
              )}
            </div>
          );
        })}
      </div>

      {action.save_to_contact && (
        <label className="flex items-center gap-2 text-xs text-muted-foreground cursor-pointer">
          <input
            type="checkbox"
            checked={saveChecked}
            onChange={(e) => setSaveChecked(e.target.checked)}
            className="rounded border-border"
          />
          {action.save_to_contact.label}
        </label>
      )}

      <div className="flex items-center gap-2 pt-1">
        <button
          type="submit"
          disabled={busy || !allValid}
          className="h-8 px-3 rounded-md bg-violet-500 text-white text-xs font-medium hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5"
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
          {action.save_to_contact && saveChecked ? "Save & continue" : "Submit"}
        </button>
        {!allValid && (
          <span className="text-[11px] text-muted-foreground">
            Required fields are missing or the format is invalid.
          </span>
        )}
      </div>
    </form>
  );
}
