/**
 * Yorik Compose — polished React port of the document-writing app.
 *
 * This is the *only* document writer in Yorik, so it has to be solid:
 *  - Pick a template on the left → server runs data_query + Jinja render
 *    → result lands in a TipTap rich-text editor.
 *  - Top toolbar: bold / italic / underline / H1-3 / lists / table / hr / undo.
 *  - Highlight text → an inline "Ask Yorik" pill appears; clicking it opens
 *    a panel that asks the LLM to rewrite the selection (formal, shorter,
 *    German, …) and offers the suggestions inline.
 *  - Right pane: source data the template pulled (transparency), live
 *    arg editor (re-render on change), and status chips.
 *  - Footer actions: Save to Paperless · Send via email · Export PDF.
 *
 * Anti-frustration choices:
 *  - Save / Send / Export are disabled until the editor has actual content.
 *  - Errors land in a non-modal toast at the bottom, never an alert().
 *  - Re-rendering the template after the user has edited prompts first —
 *    we never silently nuke their work.
 */

import {
  useCallback, useEffect, useMemo, useRef, useState,
} from "react";
import { createPortal } from "react-dom";
import { useSearchParams, useNavigate } from "react-router-dom";
import { useAuth } from "@/components/AuthGate";
import { EditorContent, useEditor } from "@tiptap/react";
import { BubbleMenu } from "@tiptap/react/menus";
import { Extension, Mark } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { Underline } from "@tiptap/extension-underline";
import { Link } from "@tiptap/extension-link";
import { TextAlign } from "@tiptap/extension-text-align";
import { Placeholder } from "@tiptap/extension-placeholder";
import { Table } from "@tiptap/extension-table";
import { TableRow } from "@tiptap/extension-table-row";
import { TableHeader } from "@tiptap/extension-table-header";
import { TableCell } from "@tiptap/extension-table-cell";
import { Image } from "@tiptap/extension-image";
import type { Editor } from "@tiptap/react";

import {
  FileText, Sparkles, Save, Send, Download, RefreshCw, Loader2,
  Bold, Italic, Underline as UnderlineIcon, Heading1, Heading2, Heading3,
  List, ListOrdered, Table as TableIcon, Minus, Undo2, Redo2,
  AlignLeft, AlignCenter, AlignRight, AlignJustify,
  X, AlertCircle, FilePlus, Wand2, CheckCircle2, Hash,
  Globe, ExternalLink, Check, ImagePlus, ChevronUp, ChevronDown,
  UsersRound, Mic, Square, Copy, Upload,
  Mail, Phone, MapPin, Trash2,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { api, type AuthMe } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Dock } from "@/components/Dock";
import {
  RecipientPicker, detectNameSuffix, findAddressKeyForName,
  type RecipientFillResult,
} from "./RecipientPicker";
import { ComposeAgentChat } from "./ComposeAgentChat";
import {
  useTriPane, MobileTopBar, MobileBackdrop,
  mobileAsideLeft, mobileAsideRight,
} from "@/components/MobileShell";
import type {
  ComposeTemplate, ComposeDraftResponse, ComposeReviseSuggestion,
  ComposeReviseResponse, NumberingMatch,
} from "./types";
import { SeriesManager } from "./SeriesManager";

type ToastKind = "info" | "success" | "error";
interface Toast { id: number; kind: ToastKind; text: string }

/**
 * Build a download-friendly filename for a generated PDF, prefering a
 * meaningful identifier from the template + args over the generic
 * template id. Pre-2026-06: every Rechnung downloaded as `rechnung-de.pdf`
 * which collided across invoices. Now: `Rechnung_2026-001.pdf`,
 * `Angebot_A-2026-007.pdf`, `Kündigung_Mustermann-GmbH.pdf`, etc.
 *
 * Safe-strings the result so it works on Windows / macOS / Linux:
 * strips path separators, control chars, and trailing dots/spaces.
 */
function composeFilename(template: any, args: Record<string, unknown> | null | undefined): string {
  const safeArgs = (args || {}) as Record<string, unknown>;
  const tags: string[] = (template?.tags || []).map((t: string) => t.toLowerCase());
  const isInvoice = tags.includes("invoice") || tags.includes("rechnung");
  const isOffer   = tags.includes("offer")   || tags.includes("angebot");
  const isCredit  = tags.includes("gutschrift");
  const isLetter  = tags.includes("letter");

  function clean(s: unknown, max = 60): string {
    return String(s ?? "")
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")  // strip combining diacritics → ASCII-safe filenames
      .replace(/[\/\\:*?"<>|]/g, "")    // strip path-illegal chars
      .replace(/\s+/g, "_")
      .replace(/[^A-Za-z0-9._\-]/g, "")
      .replace(/^[._\-]+|[._\-]+$/g, "")
      .slice(0, max);
  }

  // Invoice / offer / credit-note: prefix + number is the obvious identity.
  if (isInvoice) {
    const num = clean(safeArgs.rechnungsnummer);
    return num ? `Invoice_${num}.pdf` : "Invoice_new.pdf";
  }
  if (isOffer) {
    const num = clean(safeArgs.angebotsnummer || safeArgs.rechnungsnummer);
    return num ? `Quote_${num}.pdf` : "Quote_new.pdf";
  }
  if (isCredit) {
    const num = clean(safeArgs.gutschriftsnummer || safeArgs.rechnungsnummer);
    return num ? `CreditNote_${num}.pdf` : "CreditNote_new.pdf";
  }

  // Letters / Kündigungen / Mahnungen: <TemplateName>_<Recipient> reads
  // best in a downloads folder. Recipient lookup tries all common
  // recipient-name keys; bails to template-id if nothing's filled.
  if (isLetter) {
    const recipient = clean(
      safeArgs.vermieter_name || safeArgs.empfaenger_name || safeArgs.recipient_name
      || safeArgs.anbieter_name || safeArgs.manager_name || safeArgs.kunde_name
    );
    const base = clean(template?.name || template?.id || "Letter", 40);
    return recipient ? `${base}_${recipient}.pdf` : `${base}.pdf`;
  }

  // Fallback: template id or a generic name.
  return `${clean(template?.id) || "Document"}.pdf`;
}

// Lets templates tag a paragraph as the canonical render of a specific
// arg key (e.g. <p data-arg-key="anrede">). StarterKit's default
// Paragraph drops unknown attributes on parseHTML → renderHTML, so the
// marker is gone before we ever see it in editor.getHTML(). Adding it
// via addGlobalAttributes preserves the attribute through TipTap's
// schema, which is the load-bearing requirement for the editor → args
// bidirectional sync below — the onUpdate handler queries [data-arg-key]
// elements in the editor DOM to pull the user's typed values back into
// the args dict.
const DataArgKeyAttribute = Extension.create({
  name: "dataArgKey",
  addGlobalAttributes() {
    return [{
      types: ["paragraph"],
      attributes: {
        dataArgKey: {
          default: null,
          parseHTML: el => el.getAttribute("data-arg-key"),
          renderHTML: attrs => attrs.dataArgKey
            ? { "data-arg-key": attrs.dataArgKey }
            : {},
        },
      },
    }];
  },
});

// Preserves <span data-arg-key="..."> on inline ranges (TipTap strips
// unknown spans by default). Mirror of DataArgKeyAttribute but at the
// inline mark layer instead of the paragraph node layer — together they
// let the greeting line carry TWO independently-bound spans:
//   <p><span data-arg-key="anrede">Hallo</span> <span data-arg-key="empfaenger_name">Finanzamt München</span>,</p>
// `inclusive: true` makes the mark extend as the user types at the end
// of marked text — without it, the cursor right after "Finanzamt München"
// would type into an UNmarked region and the new characters wouldn't
// land in args.empfaenger_name.
const ArgKeyMark = Mark.create({
  name: "argKeyMark",
  inclusive: true,
  addAttributes() {
    return {
      argKey: {
        default: null,
        parseHTML: el => el.getAttribute("data-arg-key"),
        renderHTML: attrs => attrs.argKey
          ? { "data-arg-key": attrs.argKey }
          : {},
      },
    };
  },
  parseHTML() {
    return [{ tag: "span[data-arg-key]" }];
  },
  renderHTML({ HTMLAttributes }) {
    return ["span", HTMLAttributes, 0];
  },
});

// Args we sync editor → args from. Body_text is paragraph-level (one
// p per paragraph, joined with \n\n); the rest map 1:1 to either a
// single <p data-arg-key> or — for the two-span greeting line — a
// single <span data-arg-key>. Both selector shapes are handled by the
// generic [data-arg-key] selector in the onUpdate handler.
const BIDI_ARG_KEYS = [
  "anrede", "gruss", "greeting", "closing",
  "empfaenger_name", "recipient_name",
  "body_text",
] as const;

export function ComposeApp() {
  const { data: me } = useApi<AuthMe>("/api/auth/me", []);
  const role = me?.user?.role || "admin";

  const tplsApi = useApi<ComposeTemplate[]>(`/api/compose/templates?role=${encodeURIComponent(role)}`, [role]);
  const templates = tplsApi.data || [];

  // Email accounts feed the Send dialog's "From" select so the user can
  // pick which mailbox the draft goes out from. When there are 0 or 1
  // accounts the select is hidden and the backend falls back to the
  // legacy connector-store credential path.
  const emailAccountsApi = useApi<Array<{
    id: number; email: string; display_name?: string | null; is_default?: boolean;
  }>>("/api/email/accounts", []);
  const emailAccounts = emailAccountsApi.data || [];
  const defaultEmailAccountId =
    emailAccounts.find(a => a.is_default)?.id ?? emailAccounts[0]?.id;

  const [activeTemplate, setActiveTemplate] = useState<ComposeTemplate | null>(null);
  // When a draft is deep-linked (?draft_id=N) before the templates fetch
  // has resolved, loadDraftById can't find the template object yet and
  // leaves activeTemplate=null — which makes the right-side args panel
  // render the empty state even though args state is fully populated.
  // We park the desired template_id here and resolve it in an effect
  // below once the templates list arrives.
  const [pendingTemplateId, setPendingTemplateId] = useState<string | null>(null);
  const [args, setArgs] = useState<Record<string, unknown>>({});
  const [pulledData, setPulledData] = useState<Record<string, unknown> | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [editorHasContent, setEditorHasContent] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  // Numbering: which arg keys are auto-numbered + which series will be consumed
  // on Save/Send. The args panel surfaces this; Save/Send dialogs pass the
  // series IDs back to the backend so consume + audit happen atomically with
  // the document leaving Yorik.
  const [numbering, setNumbering] = useState<Record<string, NumberingMatch>>({});
  const [showSeriesManager, setShowSeriesManager] = useState(false);
  const [seriesManagerKind, setSeriesManagerKind] = useState<string | undefined>(undefined);

  // Track contact ids the user explicitly picked via RecipientPicker.
  // If the recipient name in args matches one of these, we KNOW it's
  // already saved → skip the "save this address?" prompt on Save/Send.
  const [pickedContactIds, setPickedContactIds] = useState<Set<number>>(new Set());
  // Names we already asked the user about (and they declined). Don't
  // re-prompt within the same session.
  const [dismissedRecipientNames, setDismissedRecipientNames] = useState<Set<string>>(new Set());

  // ── Sidebar tabs: Templates | Drafts ─────────────────────────────
  const [sidebarTab, setSidebarTab] = useState<"templates" | "drafts">("templates");
  // ── Right-pane tabs: Arguments | Ask Yorik ───────────────────────
  // The chat lives on the RIGHT now instead of stacked below the
  // editor — same context, more room, doesn't eat editor height.
  // When the user has no template active we default to "ask" so the
  // empty canvas immediately invites a natural-language ask.
  const [rightTab, setRightTab] = useState<"arguments" | "ask">("arguments");
  // Dismissed template-hint ids, persisted in localStorage so a hint
  // stays dismissed across reloads + sessions. Per-template — switching
  // to a different template re-shows ITS hint until the user dismisses
  // it too. Stored as a Set in memory + a comma-separated string on disk.
  const [dismissedHints, setDismissedHints] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem("yorik_compose_dismissed_hints");
      return new Set(raw ? raw.split(",").filter(Boolean) : []);
    } catch { return new Set(); }
  });
  const dismissHint = useCallback((templateId: string) => {
    setDismissedHints(prev => {
      const next = new Set(prev);
      next.add(templateId);
      try {
        localStorage.setItem("yorik_compose_dismissed_hints",
                              Array.from(next).join(","));
      } catch {}
      return next;
    });
  }, []);
  // Unread-message dot on the inactive "Ask Yorik" tab — bumped when
  // a reply lands while the user is looking at the Arguments tab.
  const [chatUnread, setChatUnread] = useState(false);
  // Tracks which saved draft is currently loaded in the editor so the
  // drafts list can highlight it. Null = blank document.
  const [activeDraftId, setActiveDraftId] = useState<number | null>(null);
  // Snapshot of editor HTML right after the most recent template/draft
  // load. Used by pickTemplate to decide if the user actually edited
  // anything — if current HTML matches this, swapping templates is
  // non-destructive and we skip the confirm dialog.
  const pristineHtmlRef = useRef<string>("");
  // When set, render the ConfirmReplaceTemplateModal. Stores the
  // pending template choice so we can apply it on confirm.
  const [pendingTemplate, setPendingTemplate] = useState<ComposeTemplate | null>(null);
  // Drafts fetch — refetched whenever a draft is created/loaded/deleted via
  // the `draftsBump` counter so the sidebar stays in sync.
  const [draftsBump, setDraftsBump] = useState(0);
  const draftsApi = useApi<Array<{
    id: number; user_id: number; kind: string; template_id: string | null;
    recipient: string | null; subject: string | null;
    created_at: string; updated_at: string;
  }>>(
    `/api/compose/saved-drafts?_=${draftsBump}`,
    [draftsBump],
  );
  const drafts = draftsApi.data || [];

  function toast(text: string, kind: ToastKind = "info") {
    const id = Date.now() + Math.random();
    setToasts(t => [...t, { id, kind, text }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 4500);
  }

  // ── editor ───────────────────────────────────────────────────────────
  // Flag consumed by the editor's onUpdate handler so server-driven
  // setContent() doesn't trigger an editor → args sync (which would be
  // a no-op at best, a feedback loop at worst). Each setContent call
  // site flips this true just before invoking the command; onUpdate
  // consumes (flips back to false) on the next fire.
  const suppressNextEditorSyncRef = useRef(false);

  const editor = useEditor({
    extensions: [
      // StarterKit 3.x bundles link + underline. Disable them here so
      // our standalone Underline + configured Link don't clash (TipTap
      // warns about duplicate extension names otherwise).
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
        link: false,
        underline: false,
      }),
      Underline,
      Link.configure({ openOnClick: false }),
      // TextAlign — without this, TipTap silently strips the inline
      // `style="text-align: right"` (or center/justify) that templates
      // use to right-align dates / center headings. Result: the editor
      // shows everything left-aligned even though the source template
      // is correct. We apply it to paragraphs + headings (the nodes
      // that carry alignment in document templates).
      TextAlign.configure({
        types: ["paragraph", "heading"],
        alignments: ["left", "center", "right", "justify"],
      }),
      Placeholder.configure({
        placeholder: "Pick a template on the left, or just start typing…",
      }),
      Table.configure({ resizable: true, HTMLAttributes: { class: "compose-table" } }),
      TableRow,
      TableHeader,
      TableCell,
      // Image with allowBase64=true so signature_data_url (data: URI,
      // typically image/png or sanitized image/svg+xml) survives the
      // TipTap parse → serialize roundtrip. Without this extension
      // ProseMirror silently strips all <img> on load, the editor
      // showed no signature, AND editor.getHTML() returned HTML with
      // no signature so the PDF download was also blank.
      Image.configure({
        allowBase64: true,
        HTMLAttributes: { class: "compose-image" },
      }),
      // Preserves <p data-arg-key="..."> attributes so the bidi sync below
      // can find which paragraph belongs to which arg. Without this the
      // attribute is dropped on the first parseHTML pass.
      DataArgKeyAttribute,
      // Same job, one layer down — preserves <span data-arg-key="..."> so
      // the two-span greeting line ("<span anrede>Hallo</span>
      // <span empfaenger_name>Finanzamt München</span>,") roundtrips
      // through TipTap with its marks intact.
      ArgKeyMark,
    ],
    content: "",
    autofocus: false,
    onUpdate: ({ editor }) => {
      setEditorHasContent(!editor.isEmpty);
      // Editor → args bidirectional sync.
      //
      // The `suppressNextEditorSync` ref is set just before every
      // editor.commands.setContent(...) call — server-driven content
      // swaps must not feed back into args (which would either be a
      // no-op or, worse, a fight against the values the user just
      // picked / typed in the args panel). After consuming the flag
      // we bail; the next genuine keystroke will pass through.
      if (suppressNextEditorSyncRef.current) {
        suppressNextEditorSyncRef.current = false;
        return;
      }
      // Pull out the text of every paragraph that the template tagged
      // with data-arg-key. The args dict is the source of truth used by
      // ArgInput and the next scheduleRerender; updating it here keeps
      // the right-pane inputs in lockstep with edits the user typed
      // directly in the lettercard. We deliberately don't call
      // scheduleRerender — the editor already has the latest content,
      // and re-rendering would set it again (potentially losing in-flight
      // edits to other paragraphs).
      const dom = editor.view.dom as HTMLElement;
      const extracted: Record<string, string> = {};
      for (const key of BIDI_ARG_KEYS) {
        // Generic selector — matches both <p data-arg-key> (paragraph-
        // level: body_text, gruss, closing) AND <span data-arg-key>
        // (inline-level: the two-span greeting line). Body_text can
        // span multiple paragraphs; everything else is single-instance.
        const els = dom.querySelectorAll(`[data-arg-key="${key}"]`);
        if (els.length === 0) continue;
        const texts = Array.from(els).map(el => (el.textContent || "").trim());
        extracted[key] = texts.length > 1 ? texts.join("\n\n") : texts[0];
      }
      if (Object.keys(extracted).length === 0) return;
      // setArgs from useState — safe to call in onUpdate; React batches.
      setArgs(prev => {
        // Skip if nothing actually changed. Avoids re-rendering AiPane
        // (and re-running every memo in there) on every keystroke that
        // doesn't move a bound arg.
        let changed = false;
        for (const [k, v] of Object.entries(extracted)) {
          if (prev[k] !== v) { changed = true; break; }
        }
        return changed ? { ...prev, ...extracted } : prev;
      });
    },
    editorProps: {
      attributes: {
        class: "compose-prose focus:outline-none",
      },
    },
  });

  // ── load draft when template / args change ───────────────────────────
  const draftTemplate = useCallback(async (tpl: ComposeTemplate, newArgs: Record<string, unknown>) => {
    if (!editor) return;
    setDrafting(true);
    try {
      // Cast: the server now also returns `numbering` and an updated `args`
      // (after auto-fill from the series engine). The shared
      // ComposeDraftResponse type may not include them — read defensively.
      const res = await api.post<ComposeDraftResponse & {
        numbering?: Record<string, NumberingMatch>;
        args?: Record<string, unknown>;
      }>(
        `/api/compose/draft?role=${encodeURIComponent(role)}`,
        { template_id: tpl.id, args: newArgs },
      );
      suppressNextEditorSyncRef.current = true;
      editor.commands.setContent(res.html || "");
      // Capture the post-setContent HTML (which TipTap may normalize)
      // as the "pristine" snapshot. pickTemplate compares against this
      // to skip the confirm dialog when the user hasn't actually edited.
      pristineHtmlRef.current = editor.getHTML();
      setEditorHasContent(!editor.isEmpty);
      setPulledData(res.data || {});
      // Pick up the server-applied args (e.g. auto-filled Rechnungsnummer)
      // so the args panel shows what's actually rendered.
      if (res.args) setArgs(res.args);
      setNumbering(res.numbering || {});
      toast(`Loaded "${tpl.name}"`, "success");
    } catch (e: any) {
      toast(`Draft failed: ${e.message}`, "error");
    } finally {
      setDrafting(false);
    }
  }, [editor, role]);

  function applyTemplate(tpl: ComposeTemplate) {
    setActiveTemplate(tpl);
    setActiveDraftId(null);
    const initialArgs = { ...(tpl.default_args || {}) };
    // Auto-prefill sender-shaped slots from the user's profile so the
    // common letterhead args (name + address + business + phone) are
    // already filled when the editor first renders. The ChatAgent
    // already does this in its context line; doing it here means the
    // manual right-rail flow gets the same treatment.
    prefillSenderArgs(initialArgs, me?.user as Record<string, unknown> | undefined);
    setArgs(initialArgs);
    draftTemplate(tpl, initialArgs);
  }

  function pickTemplate(tpl: ComposeTemplate) {
    if (activeTemplate?.id === tpl.id) return;  // same template — no-op
    // Only confirm when the user has actually edited the editor since
    // the last template/draft load. Comparing against pristineHtmlRef
    // catches the common "just clicked another template by accident"
    // case where no real work would be lost.
    const dirty = !!editor
      && !editor.isEmpty
      && editor.getHTML() !== pristineHtmlRef.current;
    if (dirty) {
      setPendingTemplate(tpl);  // opens ConfirmReplaceTemplateModal
      return;
    }
    applyTemplate(tpl);
  }

  // Chat → Compose handoff: when navigated here with ?draft_id=N
  // (clicked the "Edit →" button on a compose_draft_created card),
  // fetch the persisted draft and pre-fill the editor.
  // Runs once per draft_id change.
  const [searchParams] = useSearchParams();
  const draftIdParam = searchParams.get("draft_id");

  // Reusable: fetch a saved compose_draft by id and load it into the
  // editor + args + active template. Used by BOTH the ?draft_id= deep-
  // link path AND the inline ComposeAgentChat panel (which intercepts
  // the compose_draft_created ui_action locally instead of navigating).
  //
  // `replaceContent` semantics: when called from the inline chat,
  // ALWAYS overwrite the editor — the user just asked for it. When
  // called from a deep-link, only fill if the editor is empty (don't
  // nuke what the user is working on).
  const loadDraftById = useCallback(async (draftId: number, opts: { replaceContent?: boolean } = {}) => {
    if (!editor) return;
    try {
      const d = await api.get<{
        id: number; kind: string; template_id?: string | null;
        recipient?: string | null; subject?: string | null;
        body_html: string; args: Record<string, unknown>;
      }>(`/api/compose/saved-draft/${draftId}`);
      if (opts.replaceContent || editor.isEmpty) {
        suppressNextEditorSyncRef.current = true;
        editor.commands.setContent(d.body_html || "");
        // Snapshot for the pickTemplate dirty-check so switching
        // templates right after loading a draft (without edits) is
        // silent — no spurious "Replace?" modal.
        pristineHtmlRef.current = editor.getHTML();
        setEditorHasContent(!editor.isEmpty);
      } else {
        toast("Draft from chat loaded — kept your current edits", "info");
      }
      if (d.template_id) {
        const tpl = templates.find(t => t.id === d.template_id);
        if (tpl) {
          setActiveTemplate(tpl);
          setPendingTemplateId(null);
        } else {
          // Templates not loaded yet — park the id; the effect below
          // resolves it once tplsApi returns.
          setPendingTemplateId(d.template_id);
        }
      }
      setArgs(prev => ({
        ...prev,
        ...(d.args || {}),
        ...(d.recipient ? { recipient: d.recipient } : {}),
        ...(d.subject   ? { subject:   d.subject   } : {}),
      }));
      setActiveDraftId(d.id);
      toast(`Draft #${d.id} loaded`, "success");
    } catch (e: any) {
      toast(`Couldn't load draft: ${e.message || e}`, "error");
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor, templates]);

  useEffect(() => {
    if (!editor || !draftIdParam) return;
    const draftId = parseInt(draftIdParam, 10);
    if (!Number.isFinite(draftId) || draftId <= 0) return;
    loadDraftById(draftId, { replaceContent: false });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor, draftIdParam]);

  // Late-bind activeTemplate when templates finish loading after a
  // deep-link draft load (race condition fix). If loadDraftById ran
  // before the templates fetch resolved, pendingTemplateId carries
  // the template id the draft references; this effect picks it up
  // as soon as the templates array becomes non-empty.
  useEffect(() => {
    if (!pendingTemplateId || templates.length === 0) return;
    const tpl = templates.find(t => t.id === pendingTemplateId);
    if (tpl) {
      setActiveTemplate(tpl);
      setPendingTemplateId(null);
    }
  }, [templates, pendingTemplateId]);

  // Contacts → Compose handoff: when navigated here with ?contact_id=N
  // (clicked the "Letter" icon in /r/contacts), fetch the contact and
  // pre-fill recipient name + address into args using the same key
  // fan-out the compose_draft skill uses. Run AFTER the templates load
  // so we can pick a sensible default (a generic letter template).
  const contactIdParam = searchParams.get("contact_id");
  useEffect(() => {
    if (!contactIdParam) return;
    const cid = parseInt(contactIdParam, 10);
    if (!Number.isFinite(cid) || cid <= 0) return;
    let cancelled = false;
    (async () => {
      try {
        const c = await api.get<{
          id: number; display_name: string;
          addresses?: Array<{ kind: string; line1?: string; line2?: string;
                              postcode?: string; city?: string;
                              region?: string; country?: string }>;
        }>(`/api/contacts/${cid}`);
        if (cancelled) return;
        const addrs = c.addresses || [];
        const order = ["home", "work", "billing", "shipping"];
        const a = [...addrs].sort(
          (x, y) => order.indexOf(x.kind) - order.indexOf(y.kind),
        )[0];
        const addrStr = a ? [
          a.line1, a.line2,
          [a.postcode, a.city].filter(Boolean).join(" "),
          a.region, a.country,
        ].map(s => (s || "").trim()).filter(Boolean).join("\n") : "";
        // Fan out into the same alias keys the compose_draft skill uses
        // — so whichever template's recipient arg matches, it fills.
        const fanout: Record<string, unknown> = { recipient: c.display_name };
        for (const k of ["recipient_name", "empfaenger_name", "vermieter_name",
                          "anbieter_name", "locatore_nome"]) {
          fanout[k] = c.display_name;
        }
        if (addrStr) {
          for (const k of ["recipient_address", "recipient_address_line1",
                            "empfaenger_adresse", "vermieter_adresse",
                            "anbieter_adresse", "locatore_indirizzo"]) {
            fanout[k] = addrStr;
          }
        }
        setArgs(prev => ({ ...prev, ...fanout }));
        // Mark as picked so the "save this address?" prompt skips this
        // contact at Save/Send time.
        setPickedContactIds(prev => new Set(prev).add(c.id));
        toast(`Recipient: ${c.display_name}${addrStr ? " (address pre-filled)" : ""}`, "success");
      } catch (e: any) {
        toast(`Couldn't load contact: ${e.message || e}`, "error");
      }
    })();
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contactIdParam]);

  // Re-render with current args (no confirmation — explicit Re-render button).
  // `argsOverride` is the escape hatch for callers who just set new args via
  // a different state update (e.g. the picker filled empfaenger_name +
  // empfaenger_adresse) and need the server to see those values NOW —
  // without it the closure captures the pre-update `args`, the server
  // round-trips it back via `res.args`, and the freshly-set values get
  // wiped before the user sees them in the input fields.
  function rerender(argsOverride?: Record<string, unknown>) {
    if (!activeTemplate) return;
    draftTemplate(activeTemplate, argsOverride ?? args);
  }

  // ── Auto-rerender on user-driven arg edits ────────────────────────
  // Typing in any ArgInput (anrede, gruss, betreff, …) updates `args`
  // but the editor body only re-renders when the user manually clicks
  // Re-render. That's confusing — changes feel like they don't take
  // effect. scheduleRerender debounces to 600ms after the last
  // keystroke and fires draftTemplate with the latest args.
  //
  // Caveats:
  //  - argsRef breaks the stale-closure: we read CURRENT args at
  //    timeout time, not the closure value from when the typing started.
  //  - Only called from setArg in AiPane (i.e. user-driven edits).
  //    Imperative setArgs paths (applyTemplate, loadDraftById, contact
  //    pre-fill, server response) do NOT call this — they manage their
  //    own draftTemplate calls and would otherwise loop.
  const argsRef = useRef(args);
  useEffect(() => { argsRef.current = args; }, [args]);
  const rerenderTimerRef = useRef<number | null>(null);
  const scheduleRerender = useCallback(() => {
    if (rerenderTimerRef.current !== null) {
      window.clearTimeout(rerenderTimerRef.current);
    }
    rerenderTimerRef.current = window.setTimeout(() => {
      rerenderTimerRef.current = null;
      if (activeTemplate) draftTemplate(activeTemplate, argsRef.current);
    }, 600);
  }, [activeTemplate, draftTemplate]);
  // Cleanup any pending timer on unmount.
  useEffect(() => () => {
    if (rerenderTimerRef.current !== null) {
      window.clearTimeout(rerenderTimerRef.current);
    }
  }, []);

  // Delete a template from disk (admin only — backend enforces 403).
  // Used by the trash button in the templates sidebar. Symmetric with
  // the marketplace install: lets a user keep their template library
  // trimmed to what they actually use. Existing drafts/PDFs that were
  // rendered from this template stay; only future rendering is affected.
  async function removeTemplate(tpl: ComposeTemplate) {
    if (!confirm(`Remove "${tpl.name}" from this Yorik?\n\nExisting drafts stay; you can reinstall from the community catalogue any time.`)) return;
    try {
      await api.delete(`/api/compose/templates/${encodeURIComponent(tpl.id)}`);
      toast(`Removed "${tpl.name}"`, "success");
      if (activeTemplate?.id === tpl.id) {
        setActiveTemplate(null);
        setArgs({});
      }
      tplsApi.refetch();
    } catch (e: any) {
      toast(`Remove failed: ${e?.message || e}`, "error");
    }
  }

  // Blank doc — clear template association
  function blankDoc() {
    if (editor && !editor.isEmpty) {
      if (!confirm("Start a blank document? Your current draft will be lost.")) return;
    }
    setActiveTemplate(null);
    setArgs({});
    setPulledData(null);
    setNumbering({});
    setActiveDraftId(null);
    suppressNextEditorSyncRef.current = true;
    editor?.commands.setContent("");
    setEditorHasContent(false);
  }

  // Series IDs to consume when Save/Send fires (one per matched numbering arg)
  const seriesConsumes = useMemo(
    () => Object.values(numbering).map(n => n.series_id),
    [numbering],
  );

  // Prefill the Send dialog's "To" field from the arg tagged with
  // role=recipient_email. Looked up by role (not by key name) so DE
  // `empfaenger_email`, EN `recipient_email`, and any future locale
  // all work without per-template plumbing.
  const recipientEmailDefault = useMemo(() => {
    const schema = activeTemplate?.ask_user_for_args || [];
    const key = schema.find(f => f.role === "recipient_email")?.key;
    if (!key) return "";
    const v = args[key];
    return typeof v === "string" ? v : "";
  }, [activeTemplate, args]);

  // SendDialog's subject field — read the arg whose role is "subject"
  // (DE templates use `betreff`, EN `subject`, role lookup beats key-name
  // guessing). Falls back to the template name only when no subject arg
  // exists or it's still empty; previously the dialog always pre-filled
  // with the template name, ignoring whatever the user had typed in the
  // betreff input on the right pane.
  const subjectDefault = useMemo(() => {
    const schema = activeTemplate?.ask_user_for_args || [];
    const key = schema.find(f => f.role === "subject")?.key;
    const fallback = activeTemplate?.name || "Document";
    if (!key) return fallback;
    const v = args[key];
    return (typeof v === "string" && v.trim()) ? v : fallback;
  }, [activeTemplate, args]);

  // ── save / send / export ─────────────────────────────────────────────
  const [saveBusy, setSaveBusy] = useState(false);
  const [sendBusy, setSendBusy] = useState(false);
  const [pdfBusy, setPdfBusy] = useState(false);
  const [showSaveDialog, setShowSaveDialog] = useState(false);
  const [showSendDialog, setShowSendDialog] = useState(false);
  const [showCommunityDialog, setShowCommunityDialog] = useState(false);

  async function exportPdf() {
    if (!editor) return;
    setPdfBusy(true);
    try {
      const fname = composeFilename(activeTemplate, args);
      const res = await fetch(`/api/compose/render-pdf?role=${encodeURIComponent(role)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          body_html: editor.getHTML(),
          filename: fname,
          template_id: activeTemplate?.id || null,
          args: activeTemplate ? args : {},
        }),
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => `HTTP ${res.status}`);
        throw new Error(detail.slice(0, 200));
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast("PDF downloaded", "success");
    } catch (e: any) {
      toast(`PDF render failed: ${e.message}`, "error");
    } finally {
      setPdfBusy(false);
    }
  }

  // ── "save this address?" prompt ───────────────────────────────────
  // After a successful Save/Send, scan the args for name+address pairs
  // that look like a recipient but weren't picked from the contacts hub.
  // If any pair is fresh (not already on a contact), surface a sticky
  // toast offering to save it.
  const argKeysSnapshot = useCallback(() => Object.keys(args || {}), [args]);

  const findUnsavedRecipientPair = useCallback(async () => {
    const allKeys = argKeysSnapshot();
    if (allKeys.length === 0) return null;
    for (const nk of allKeys) {
      if (!detectNameSuffix(nk)) continue;
      const nameVal = String((args[nk] ?? "")).trim();
      if (!nameVal) continue;
      if (dismissedRecipientNames.has(nameVal.toLowerCase())) continue;
      const addrKey = findAddressKeyForName(nk, allKeys);
      const addrVal = addrKey ? String((args[addrKey] ?? "")).trim() : "";
      // No address → not enough to save a useful contact.
      if (!addrVal) continue;

      // Quick existence check — search by name. If any active contact
      // shares the display name, treat as already saved (cheap heuristic).
      try {
        const matches = await api.get<Array<{ id: number; display_name: string }>>(
          `/api/contacts?status=active&q=${encodeURIComponent(nameVal)}&limit=5`,
        );
        const already = matches.some(m =>
          m.display_name.trim().toLowerCase() === nameVal.toLowerCase()
          || pickedContactIds.has(m.id)
        );
        if (already) continue;
      } catch {
        // Search failure shouldn't block save flow — skip the prompt.
        return null;
      }

      return { name: nameVal, address: addrVal };
    }
    return null;
  }, [args, argKeysSnapshot, dismissedRecipientNames, pickedContactIds]);

  // Sticky toast prompting "save this address?". Distinct from the
  // ephemeral `toast()` because we want it to stay until the user picks.
  const [saveAddressPrompt, setSaveAddressPrompt] =
    useState<{ name: string; address: string } | null>(null);

  async function maybePromptSaveRecipient() {
    const pair = await findUnsavedRecipientPair();
    if (pair) setSaveAddressPrompt(pair);
  }

  async function confirmSaveRecipient() {
    if (!saveAddressPrompt) return;
    const { name, address } = saveAddressPrompt;
    try {
      // Create the contact with display_name, then split the address
      // back into rough postcode/city if we can — best-effort, the user
      // can refine in /r/contacts. We only require line1 + city.
      const lines = address.split("\n").map(s => s.trim()).filter(Boolean);
      const line1 = lines[0] || "";
      // Heuristic: line that starts with 4-5 digits is the postcode line
      // (DE/PL/IT all match this loosely). Anything after is city.
      let postcode: string | undefined;
      let city: string | undefined;
      for (let i = 1; i < lines.length; i++) {
        const ln = lines[i];
        const m = ln.match(/^(\d{4,5})\s+(.+)$/);
        if (m) { postcode = m[1]; city = m[2]; break; }
      }
      if (!city && lines.length > 1) city = lines[lines.length - 1];

      const contact = await api.post<{ id: number }>("/api/contacts", {
        display_name: name,
        kind: /\bgmbh|llc|ltd|inc|s\.?p\.?a\.?|s\.?r\.?l\.?\b/i.test(name) ? "business" : "person",
        status: "active",
        source: "compose",
      });
      await api.post(`/api/contacts/${contact.id}/addresses`, {
        kind: "home",
        line1,
        postcode,
        city,
      });
      toast(`Saved "${name}" to your contacts`, "success");
      setPickedContactIds(prev => new Set(prev).add(contact.id));
    } catch (e: any) {
      toast(`Save failed: ${e.message || e}`, "error");
    } finally {
      setSaveAddressPrompt(null);
    }
  }

  function dismissSaveRecipient() {
    if (saveAddressPrompt) {
      setDismissedRecipientNames(prev => {
        const next = new Set(prev);
        next.add(saveAddressPrompt.name.toLowerCase());
        return next;
      });
    }
    setSaveAddressPrompt(null);
  }

  async function saveToPaperless(payload: { title: string; tags: string[] }) {
    if (!editor) return;
    setSaveBusy(true);
    try {
      const r = await api.post<{ series_allocations?: Array<{ formatted: string }> }>(
        `/api/compose/save?role=${encodeURIComponent(role)}`,
        {
          body_html: editor.getHTML(),
          title: payload.title,
          tags: payload.tags,
          series_consumes: seriesConsumes.length > 0 ? seriesConsumes : undefined,
        },
      );
      const allocated = r.series_allocations?.map(a => a.formatted).join(", ");
      toast(
        allocated
          ? `Saved "${payload.title}" · allocated ${allocated}`
          : `Saved "${payload.title}" to Paperless`,
        "success",
      );
      setShowSaveDialog(false);
      // After consume, the draft's preview number is stale — re-render so
      // the next allocation is shown.
      if (allocated && activeTemplate) rerender();
      // Surface the "save this address?" prompt if the recipient was
      // typed manually (and isn't on a contact yet).
      void maybePromptSaveRecipient();
    } catch (e: any) {
      toast(`Save failed: ${e.message}`, "error");
    } finally {
      setSaveBusy(false);
    }
  }

  async function sendEmail(payload: {
    to: string; subject: string; body_text: string; title: string;
    tags: string[]; also_save: boolean;
    delivery: "attachment" | "inline";
    account_id?: number;
  }) {
    if (!editor) return;
    setSendBusy(true);
    try {
      const r = await api.post<{ series_allocations?: Array<{ formatted: string }> }>(
        `/api/compose/send-email?role=${encodeURIComponent(role)}`,
        {
          body_html: editor.getHTML(),
          ...payload,
          series_consumes: seriesConsumes.length > 0 ? seriesConsumes : undefined,
        },
      );
      const allocated = r.series_allocations?.map(a => a.formatted).join(", ");
      toast(
        allocated
          ? `Sent to ${payload.to} · allocated ${allocated}`
          : `Sent to ${payload.to}`,
        "success",
      );
      setShowSendDialog(false);
      if (allocated && activeTemplate) rerender();
      void maybePromptSaveRecipient();
    } catch (e: any) {
      toast(`Send failed: ${e.message}`, "error");
    } finally {
      setSendBusy(false);
    }
  }

  // ── render ───────────────────────────────────────────────────────────
  // pb-16 keeps the action footer (Export / Save / Send) clear of the
  // floating Dock pill. Without it, buttons clip behind the dock on
  // narrower viewports.
  const tri = useTriPane();

  return (
    <div className="flex h-screen bg-background text-foreground pb-16 relative">
      <MobileBackdrop show={tri.leftOpen || tri.rightOpen} onClick={tri.closeAll} />
      {/* ── Templates ───────────────────────────────────────── */}
      <aside className={cn(
        "w-[280px] border-r border-border flex flex-col bg-sidebar shrink-0",
        mobileAsideLeft(tri.leftOpen),
      )}>
        <header className="h-16 px-5 flex items-center justify-between border-b border-border">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-full bg-rose-500/15 flex items-center justify-center">
              <FilePlus className="w-4 h-4 text-rose-500" />
            </div>
            <div>
              <div className="font-semibold leading-none">Compose</div>
              <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5">
                {templates.length} template{templates.length === 1 ? "" : "s"}
              </div>
            </div>
          </div>
          <button
            onClick={blankDoc}
            title="Blank document"
            className="w-8 h-8 inline-flex items-center justify-center rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition"
          >
            <FileText className="w-4 h-4" />
          </button>
        </header>

        {/* Community templates hero — primary CTA for a fresh user
            to seed their template library. Hand-written templates can
            also be dropped into templates/<id>.json directly. */}
        <div className="px-3 pt-3">
          <button
            onClick={() => setShowCommunityDialog(true)}
            className={cn(
              "w-full inline-flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg text-sm font-medium transition shadow-sm",
              "bg-gradient-to-r from-rose-500 to-violet-500 hover:from-rose-600 hover:to-violet-600 text-white",
            )}
            title="Pull pre-made templates from the yorik-community GitHub catalogue"
          >
            <Globe className="w-4 h-4" /> Browse community templates
          </button>
        </div>

        {/* Tabs — Templates vs Drafts. Persisted in component state only
            (no URL sync) since tab choice is ephemeral. Active tab gets a
            rose-tinted underline + bold weight to match the rest of the
            Compose chrome. */}
        <div className="px-3 pt-3">
          <div className="flex items-center gap-1 border-b border-border">
            <SidebarTabButton
              active={sidebarTab === "templates"}
              onClick={() => setSidebarTab("templates")}
              label="Templates"
              count={templates.length}
            />
            <SidebarTabButton
              active={sidebarTab === "drafts"}
              onClick={() => setSidebarTab("drafts")}
              label="Drafts"
              count={drafts.length}
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-2 space-y-1 pt-3">
          {sidebarTab === "templates" && (
            <>
              {tplsApi.loading && templates.length === 0 && (
                <div className="px-2 space-y-3 pt-2">
                  {[1,2,3].map(i => (
                    <div key={i} className="p-3 animate-pulse space-y-2">
                      <div className="h-3 bg-muted/60 rounded w-2/3" />
                      <div className="h-2.5 bg-muted/40 rounded w-full" />
                    </div>
                  ))}
                </div>
              )}
              {!tplsApi.loading && templates.length === 0 && (
                <div className="px-4 py-8 text-center">
                  <div className="w-12 h-12 mx-auto mb-3 rounded-2xl bg-gradient-to-br from-rose-500/20 to-violet-500/20 flex items-center justify-center">
                    <Sparkles className="w-5 h-5 text-rose-500" />
                  </div>
                  <div className="text-sm font-medium text-foreground mb-1">No templates yet</div>
                  <div className="text-[11px] text-muted-foreground leading-relaxed">
                    Use <strong className="text-foreground/80">Browse community templates</strong> above
                    to pick a ready-made template (invoice, quote, letter…), or drop
                    a JSON template into <code className="text-foreground/80">templates/</code>.
                  </div>
                </div>
              )}
              {templates.map(tpl => {
                const isActive = activeTemplate?.id === tpl.id;
                return (
                  <div key={tpl.id} className="relative group">
                    <button
                      onClick={() => pickTemplate(tpl)}
                      className={cn(
                        "w-full text-left px-3 py-2.5 rounded-lg transition",
                        isActive ? "bg-sidebar-accent shadow-sm" : "hover:bg-sidebar-accent/50",
                      )}
                    >
                      <div className="flex items-center gap-2">
                        <div className={cn(
                          "w-7 h-7 rounded-md flex items-center justify-center shrink-0",
                          isActive
                            ? "bg-rose-500/20 text-rose-500"
                            : "bg-muted/60 text-muted-foreground group-hover:text-foreground",
                        )}>
                          <FileText className="w-3.5 h-3.5" />
                        </div>
                        <div className="flex-1 min-w-0 pr-6">
                          <div className="text-sm font-medium truncate">{tpl.name}</div>
                          {tpl.vertical && (
                            <div className="text-[10px] text-muted-foreground mt-0.5 truncate">
                              {tpl.vertical}
                            </div>
                          )}
                        </div>
                      </div>
                      {tpl.description && (
                        <div className="text-[11px] text-muted-foreground mt-1.5 line-clamp-2 leading-snug">
                          {tpl.description}
                        </div>
                      )}
                      {(tpl.tags?.length || 0) > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1.5">
                          {tpl.tags.slice(0, 3).map(t => (
                            <span key={t} className="text-[9px] uppercase tracking-wider bg-muted/60 text-muted-foreground px-1.5 py-0.5 rounded">
                              {t}
                            </span>
                          ))}
                        </div>
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); removeTemplate(tpl); }}
                      title={`Remove "${tpl.name}" from this Yorik`}
                      className="absolute top-2 right-2 p-1 rounded-md text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10 opacity-0 group-hover:opacity-100 focus:opacity-100 transition"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                );
              })}
            </>
          )}

          {sidebarTab === "drafts" && (
            <DraftsListPanel
              drafts={drafts}
              loading={draftsApi.loading}
              activeDraftId={activeDraftId}
              onPick={(id) => loadDraftById(id, { replaceContent: true })}
              onDelete={async (id) => {
                if (!confirm("Delete this draft?")) return;
                try {
                  await api.delete(`/api/compose/saved-draft/${id}`);
                  toast("Draft deleted.", "success");
                  setDraftsBump(n => n + 1);
                  if (activeDraftId === id) {
                    setActiveDraftId(null);
                    suppressNextEditorSyncRef.current = true;
                    editor?.commands.setContent("");
                    setActiveTemplate(null);
                    setArgs({});
                  }
                } catch (e: any) {
                  toast(`Delete failed: ${e.message || e}`, "error");
                }
              }}
            />
          )}
        </div>

        <footer className="border-t border-border px-4 py-3 text-xs text-muted-foreground flex items-center justify-between">
          <span className="truncate">{me?.user?.name ? `Signed in · ${role}` : "Loading…"}</span>
          <button
            onClick={() => tplsApi.refetch()}
            className="text-muted-foreground hover:text-foreground transition"
            title="Reload templates"
          >
            <RefreshCw className={cn("w-3.5 h-3.5", tplsApi.loading && "animate-spin")} />
          </button>
        </footer>
      </aside>

      {/* ── Editor ──────────────────────────────────────────── */}
      <section className="flex-1 flex flex-col bg-background min-w-0 compose-bg">
        <MobileTopBar
          title={activeTemplate?.name || "Compose"}
          onMenuClick={() => tri.setLeftOpen(true)}
          onContextClick={activeTemplate ? () => tri.setRightOpen(true) : undefined}
          contextLabel="Template data"
        />
        <Toolbar editor={editor} />
        <div className="flex-1 overflow-y-auto px-2 sm:px-6 lg:px-8 py-3 sm:py-6">
          <div className="max-w-[820px] mx-auto bg-card border border-border rounded-2xl shadow-sm relative overflow-hidden">
            {drafting && (
              <div className="absolute inset-0 bg-card/80 backdrop-blur-sm z-10 flex items-center justify-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="w-4 h-4 animate-spin" /> Drafting…
              </div>
            )}
            {/* Inner padding shrinks aggressively on phone — at 375px
                viewport the outer px-2 + this px-4 leaves ~340px of
                typeable width vs the old ~230px (px-8 + px-10).
                Desktop layout unchanged via sm:/lg: prefixes. */}
            <div className="px-4 sm:px-8 lg:px-10 py-6 sm:py-8 lg:py-10">
              <EditorContent editor={editor} />
            </div>
          </div>
        </div>

        {/* Highlight-and-ask floating pill */}
        {editor && (
          <SelectionPill
            editor={editor}
            role={role}
            toast={toast}
            args={args}
            activeTemplate={activeTemplate}
            senderName={me?.user?.name || ""}
            senderBusiness={(me?.user as any)?.business_name || ""}
          />
        )}

        {/* Editor-only notes from the active template — usage hints,
            legal-context disclaimers, Einschreiben reminders. Lives
            BELOW the editable area so it can't be edited into the
            document body (the previous in-body approach lost its CSS
            class through TipTap's parsing and leaked into PDFs).
            Dismissible per-template via the X button; the dismissal
            persists in localStorage so the user doesn't have to
            re-close it every session. Switching to a different
            template surfaces ITS hint again until dismissed. */}
        {activeTemplate?.editor_notes
          && !dismissedHints.has(activeTemplate.id) && (
          <div className="border-t border-border bg-amber-50 dark:bg-amber-950/20 shrink-0">
            <div className="max-w-[820px] mx-auto px-6 py-3">
              <div className="flex items-start gap-2.5">
                <span className="text-base leading-none mt-0.5">📝</span>
                <div className="flex-1 min-w-0">
                  <div className="text-[11px] uppercase tracking-wider text-amber-700 dark:text-amber-400 font-semibold mb-1">
                    About this template
                  </div>
                  <div
                    className="text-xs text-amber-900 dark:text-amber-100/90 leading-relaxed [&>p]:m-0 [&>p+p]:mt-1.5 [&>p>strong]:font-semibold"
                    dangerouslySetInnerHTML={{ __html: activeTemplate.editor_notes }}
                  />
                </div>
                <button
                  type="button"
                  onClick={() => dismissHint(activeTemplate.id)}
                  className="shrink-0 -mr-1 w-9 h-9 md:w-7 md:h-7 inline-flex items-center justify-center rounded-md text-amber-700/70 dark:text-amber-400/70 hover:text-amber-700 dark:hover:text-amber-400 hover:bg-amber-500/10 transition"
                  title="Hide hint (stays hidden for this template)"
                  aria-label="Hide hint"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>
          </div>
        )}

        <SignatureUpsellBanner />

        {/* Footer actions */}
        <ComposeFooter
          activeTemplate={activeTemplate}
          args={args}
          editorHasContent={editorHasContent}
          pdfBusy={pdfBusy}
          saveBusy={saveBusy}
          sendBusy={sendBusy}
          onExportPdf={exportPdf}
          onOpenSave={() => setShowSaveDialog(true)}
          onOpenSend={() => setShowSendDialog(true)}
        />
      </section>

      {/* ── Right pane: Arguments | Ask Yorik (tabbed) ────────────────
          Chat used to live below the editor where it ate vertical
          space and felt like an afterthought. Moving it into the
          right pane gives it the same surface area as the structured
          form — and matches the side-by-side model Claude artifacts
          / v0 / Cursor compose all converged on. */}
      <aside className={cn(
        // max-w-[88vw] clamp leaves a small peek of the underlying
        // editor on phones (375px viewport: 420px would have extended
        // past the viewport entirely, hiding the backdrop hint).
        "w-[420px] max-w-[88vw] md:max-w-none border-l border-border flex flex-col bg-card shrink-0",
        mobileAsideRight(tri.rightOpen),
      )}>
        <div className="h-16 px-3 flex items-center gap-1 border-b border-border shrink-0">
          <RightPaneTab
            label="Arguments"
            active={rightTab === "arguments"}
            onClick={() => setRightTab("arguments")}
          />
          <RightPaneTab
            label="Ask Yorik"
            active={rightTab === "ask"}
            unread={chatUnread}
            onClick={() => { setRightTab("ask"); setChatUnread(false); }}
          />
        </div>

        <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
          {rightTab === "arguments" && (
            activeTemplate ? (
              <AiPane
                template={activeTemplate}
                args={args}
                pulledData={pulledData}
                numbering={numbering}
                onArgsChange={setArgs}
                onRerender={rerender}
                onScheduleRerender={scheduleRerender}
                drafting={drafting}
                onContactPicked={(r) => setPickedContactIds(prev => new Set(prev).add(r.contactId))}
                onOpenNumbering={(kind) => {
                  setSeriesManagerKind(kind);
                  setShowSeriesManager(true);
                }}
                role={role}
                toast={toast}
                senderName={me?.user?.name || ""}
                senderBusiness={(me?.user as any)?.business_name || ""}
              />
            ) : (
              <EmptyAi
                onOpenNumbering={() => { setSeriesManagerKind(undefined); setShowSeriesManager(true); }}
                templateCount={templates.length}
              />
            )
          )}
          {rightTab === "ask" && (
            <ComposeAgentChat
              templateId={activeTemplate?.id ?? null}
              templateName={activeTemplate?.name ?? null}
              args={args}
              bodyHtml={editor?.getHTML() ?? ""}
              draftId={activeDraftId}
              onDraftLoaded={(draftId) => {
                loadDraftById(draftId, { replaceContent: true });
                setDraftsBump(n => n + 1);
              }}
              toast={toast}
              role={role}
              embedded
              onAssistantMessage={() => {
                // Quiet dot when the user is already on the chat tab.
                if (rightTab !== "ask") setChatUnread(true);
              }}
            />
          )}
        </div>
      </aside>

      {/* "Save this address?" prompt — sticky, sits above the ephemeral
          toast stack. Distinct from the regular toasts because it needs
          a yes/no decision and shouldn't auto-dismiss. */}
      {saveAddressPrompt && (
        <div className="fixed bottom-32 right-6 z-[1200] max-w-sm bg-card border border-amber-500/40 rounded-lg shadow-xl p-3 animate-in slide-in-from-right">
          <div className="text-xs font-semibold text-amber-600 flex items-center gap-1.5 mb-1">
            <Sparkles className="w-3 h-3" /> Save to contacts?
          </div>
          <div className="text-xs text-foreground mb-1">
            <span className="font-medium">{saveAddressPrompt.name}</span> isn't on your contacts list.
          </div>
          <div className="text-[10px] text-muted-foreground whitespace-pre-line mb-3 line-clamp-3 leading-tight">
            {saveAddressPrompt.address}
          </div>
          <div className="flex gap-2 justify-end">
            <button
              onClick={dismissSaveRecipient}
              className="text-[11px] px-2.5 py-1 rounded-md border border-border bg-card hover:bg-muted text-muted-foreground"
            >
              Not now
            </button>
            <button
              onClick={confirmSaveRecipient}
              className="text-[11px] px-2.5 py-1 rounded-md bg-amber-500 text-white hover:opacity-90 font-medium"
            >
              Save contact
            </button>
          </div>
        </div>
      )}

      {/* Toasts */}
      <div className="fixed bottom-20 right-6 z-[1100] flex flex-col gap-2">
        {toasts.map(t => (
          <div
            key={t.id}
            className={cn(
              "px-4 py-2.5 rounded-lg shadow-lg border text-sm font-medium animate-in slide-in-from-right",
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

      {/* Dialogs */}
      {showSaveDialog && (
        <SaveDialog
          defaultTitle={activeTemplate?.name || "Document"}
          defaultTags={["compose", activeTemplate?.id || ""].filter(Boolean)}
          busy={saveBusy}
          onSave={saveToPaperless}
          onClose={() => setShowSaveDialog(false)}
        />
      )}
      {showSendDialog && (
        <SendDialog
          defaultTo={recipientEmailDefault}
          defaultSubject={subjectDefault}
          defaultTitle={activeTemplate?.id || "document"}
          defaultTags={["compose", activeTemplate?.id || ""].filter(Boolean)}
          defaultDelivery={
            (activeTemplate?.delivery_default === "inline" ? "inline" : "attachment")
          }
          accounts={emailAccounts}
          defaultAccountId={defaultEmailAccountId}
          busy={sendBusy}
          onSend={sendEmail}
          onClose={() => setShowSendDialog(false)}
        />
      )}
      {showCommunityDialog && (
        <CommunityTemplatesDialog
          installedIds={new Set(templates.map(t => t.id))}
          onInstalled={() => { tplsApi.refetch(); }}
          onClose={() => setShowCommunityDialog(false)}
          toast={toast}
        />
      )}
      {showSeriesManager && (
        <SeriesManager
          initialKind={seriesManagerKind}
          onClose={() => setShowSeriesManager(false)}
          onChanged={() => { if (activeTemplate) rerender(); }}
          toast={toast}
        />
      )}
      {pendingTemplate && (
        <ConfirmReplaceTemplateModal
          incomingName={pendingTemplate.name}
          currentName={activeTemplate?.name || "current document"}
          onConfirm={() => {
            const t = pendingTemplate;
            setPendingTemplate(null);
            if (t) applyTemplate(t);
          }}
          onCancel={() => setPendingTemplate(null)}
        />
      )}

      {/* Mobile FAB — opens the right drawer directly to "Ask Yorik".
          The chat IS the primary input for Compose (you tell Yorik
          what to write, he fills the template) — but on mobile it
          lives in a drawer with no breadcrumb hinting at it. This
          FAB makes the AI entry discoverable. Sits BOTTOM-LEFT to
          match the calendar/email/contacts FAB convention (avoids
          the right-side VoiceFab). Hidden when the drawer is
          already open. */}
      {!tri.rightOpen && (
        <button
          type="button"
          onClick={() => { setRightTab("ask"); tri.setRightOpen(true); setChatUnread(false); }}
          className="md:hidden fixed left-4 bottom-[max(5.5rem,calc(env(safe-area-inset-bottom)+4.5rem))] z-30 w-14 h-14 rounded-full bg-violet-500 text-white shadow-lg flex items-center justify-center hover:opacity-90 active:scale-95 transition"
          aria-label="Ask Yorik to draft"
          title="Ask Yorik"
        >
          <Sparkles className="w-5 h-5" />
          {chatUnread && (
            <span className="absolute top-1 right-1 w-2.5 h-2.5 rounded-full bg-rose-500 ring-2 ring-violet-500" />
          )}
        </button>
      )}

      <Dock activeAppId="compose" />

      <style>{`
        .compose-bg {
          background-image:
            radial-gradient(circle at 30% 15%, hsl(0 80% 60% / 0.05), transparent 50%),
            radial-gradient(circle at 70% 85%, hsl(263 50% 60% / 0.04), transparent 50%);
        }
        /* Editor prose styling — uses Tailwind palette so dark mode works. */
        .compose-prose { min-height: 560px; font-size: 14.5px; line-height: 1.65; color: hsl(var(--foreground)); }
        .compose-prose p { margin: 0.5em 0; }
        .compose-prose h1 { font-size: 28px; font-weight: 700; margin: 0.6em 0 0.4em; }
        .compose-prose h2 { font-size: 22px; font-weight: 600; margin: 0.6em 0 0.3em; }
        .compose-prose h3 { font-size: 17px; font-weight: 600; margin: 0.5em 0 0.2em; }
        /* Mobile: the forced 560px min-height ate ~84% of an iPhone SE
           viewport on a blank doc, pushing the footer + status chip
           off-screen. Plus the 28px h1 on a now-330px-wide column
           wrapped at 6-7 characters. Both relaxed on mobile only. */
        @media (max-width: 640px) {
          .compose-prose { min-height: 320px; font-size: 15px; }
          .compose-prose h1 { font-size: 22px; }
          .compose-prose h2 { font-size: 18px; }
          .compose-prose h3 { font-size: 16px; }
        }
        .compose-prose ul, .compose-prose ol { padding-left: 1.4em; margin: 0.4em 0; }
        .compose-prose ul { list-style: disc; }
        .compose-prose ol { list-style: decimal; }
        .compose-prose blockquote {
          border-left: 3px solid hsl(var(--border));
          padding-left: 1em;
          color: hsl(var(--muted-foreground));
          margin: 0.6em 0;
        }
        .compose-prose code {
          background: hsl(var(--muted));
          padding: 0.1em 0.35em;
          border-radius: 4px;
          font-size: 0.9em;
        }
        .compose-prose hr {
          border: 0;
          border-top: 1px solid hsl(var(--border));
          margin: 1.4em 0;
        }
        .compose-prose .compose-table, .compose-prose table {
          border-collapse: collapse;
          width: 100%;
          margin: 0.8em 0;
        }
        .compose-prose table th, .compose-prose table td {
          border: 1px solid hsl(var(--border));
          padding: 0.45em 0.7em;
          font-size: 0.95em;
        }
        .compose-prose table th { background: hsl(var(--muted) / 0.6); font-weight: 600; text-align: left; }
        .compose-prose table .total { font-weight: 700; }
        .compose-prose table .right { text-align: right; }
        .compose-prose .small { font-size: 0.85em; color: hsl(var(--muted-foreground)); }
        .compose-prose .ProseMirror-focused { outline: none; }
        .compose-prose p.is-editor-empty:first-child::before {
          color: hsl(var(--muted-foreground));
          content: attr(data-placeholder);
          float: left;
          height: 0;
          pointer-events: none;
        }
        /* Very subtle inline highlight on bound arg spans (the two-span
           greeting line: anrede + recipient name). Hints at "this is a
           bound field" without yelling "form input". Scoped to spans so
           paragraph-level data-arg-key blocks (body_text, gruss) stay
           untouched. */
        .compose-prose span[data-arg-key] {
          background-color: hsl(var(--accent) / 0.06);
          border-radius: 2px;
          padding: 0 2px;
          margin: 0 -2px;
          transition: background-color 0.15s ease;
        }
        .compose-prose span[data-arg-key]:hover {
          background-color: hsl(var(--accent) / 0.12);
        }
      `}</style>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Toolbar
// ---------------------------------------------------------------------------

function Toolbar({ editor }: { editor: Editor | null }) {
  const imageInputRef = useRef<HTMLInputElement>(null);

  if (!editor) return (
    <div className="h-12 border-b border-border bg-background/80 backdrop-blur shrink-0" />
  );
  // Capture the now-non-null editor in a local so TS narrowing survives
  // closures (FileReader.onload, useMemo callback, etc.).
  const ed: Editor = editor;

  function onImagePicked(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (f) {
      const ok = new Set(["image/png","image/jpeg","image/gif","image/webp","image/svg+xml"]);
      if (!ok.has(f.type)) {
        alert("Please use a PNG, JPEG, GIF, WebP or SVG image.");
      } else if (f.size > 5 * 1024 * 1024) {
        alert(`Image is too large (${Math.round(f.size/1024)} KB). Max 5 MB.`);
      } else {
        const reader = new FileReader();
        reader.onload = () => {
          const url = String(reader.result || "");
          ed.chain().focus().setImage({ src: url, alt: f.name }).run();
        };
        reader.readAsDataURL(f);
      }
    }
    // Reset so picking the same file twice still triggers onChange.
    if (imageInputRef.current) imageInputRef.current.value = "";
  }

  const groups = useMemo(() => [
    [
      { icon: Bold, action: () => ed.chain().focus().toggleBold().run(),       isActive: (): boolean => ed.isActive("bold"),       title: "Bold (⌘B)" },
      { icon: Italic, action: () => ed.chain().focus().toggleItalic().run(),   isActive: (): boolean => ed.isActive("italic"),     title: "Italic (⌘I)" },
      { icon: UnderlineIcon, action: () => ed.chain().focus().toggleUnderline().run(), isActive: (): boolean => ed.isActive("underline"), title: "Underline (⌘U)" },
    ],
    [
      { icon: Heading1, action: () => ed.chain().focus().toggleHeading({ level: 1 }).run(), isActive: (): boolean => ed.isActive("heading", { level: 1 }), title: "Heading 1" },
      { icon: Heading2, action: () => ed.chain().focus().toggleHeading({ level: 2 }).run(), isActive: (): boolean => ed.isActive("heading", { level: 2 }), title: "Heading 2" },
      { icon: Heading3, action: () => ed.chain().focus().toggleHeading({ level: 3 }).run(), isActive: (): boolean => ed.isActive("heading", { level: 3 }), title: "Heading 3" },
    ],
    [
      { icon: List, action: () => ed.chain().focus().toggleBulletList().run(),       isActive: (): boolean => ed.isActive("bulletList"), title: "Bulleted list" },
      { icon: ListOrdered, action: () => ed.chain().focus().toggleOrderedList().run(), isActive: (): boolean => ed.isActive("orderedList"), title: "Numbered list" },
    ],
    [
      { icon: AlignLeft,    action: () => ed.chain().focus().setTextAlign("left").run(),    isActive: (): boolean => ed.isActive({ textAlign: "left" }) || (!ed.isActive({ textAlign: "center" }) && !ed.isActive({ textAlign: "right" }) && !ed.isActive({ textAlign: "justify" })), title: "Align left" },
      { icon: AlignCenter,  action: () => ed.chain().focus().setTextAlign("center").run(),  isActive: (): boolean => ed.isActive({ textAlign: "center" }),  title: "Align center" },
      { icon: AlignRight,   action: () => ed.chain().focus().setTextAlign("right").run(),   isActive: (): boolean => ed.isActive({ textAlign: "right" }),   title: "Align right" },
      { icon: AlignJustify, action: () => ed.chain().focus().setTextAlign("justify").run(), isActive: (): boolean => ed.isActive({ textAlign: "justify" }), title: "Justify" },
    ],
    [
      { icon: TableIcon, action: () => ed.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run(), isActive: (): boolean => false, title: "Insert table" },
      { icon: Minus, action: () => ed.chain().focus().setHorizontalRule().run(),       isActive: (): boolean => false, title: "Horizontal rule" },
      { icon: ImagePlus, action: () => imageInputRef.current?.click(), isActive: (): boolean => false, title: "Insert image (or just drag into the document)" },
    ],
    [
      { icon: Undo2, action: () => ed.chain().focus().undo().run(), isActive: (): boolean => false, title: "Undo (⌘Z)", disabled: (): boolean => !ed.can().undo() },
      { icon: Redo2, action: () => ed.chain().focus().redo().run(), isActive: (): boolean => false, title: "Redo (⌘⇧Z)", disabled: (): boolean => !ed.can().redo() },
    ],
  ], [editor]);

  // Toolbar height + button sizes bumped on mobile so bold/italic/
  // H1/list mis-taps don't derail writing. Desktop keeps the
  // compact h-12 / w-8 buttons.
  return (
    <div className="h-14 md:h-12 border-b border-border bg-background/80 backdrop-blur shrink-0 flex items-center px-2 md:px-4 gap-1 overflow-x-auto">
      {groups.map((group, gi) => (
        <div key={gi} className="flex items-center gap-0.5 shrink-0">
          {gi > 0 && <div className="w-px h-5 bg-border/60 mx-1 md:mx-1.5" />}
          {group.map((btn, bi) => {
            const active = btn.isActive();
            const disabled = (btn as any).disabled?.() ?? false;
            const Icon = btn.icon;
            return (
              <button
                key={bi}
                onMouseDown={e => e.preventDefault()}
                onClick={btn.action}
                disabled={disabled}
                title={btn.title}
                aria-label={btn.title}
                className={cn(
                  "w-10 h-10 md:w-8 md:h-8 inline-flex items-center justify-center rounded-md transition shrink-0",
                  active && "bg-muted text-foreground",
                  !active && !disabled && "text-muted-foreground hover:bg-muted/70 hover:text-foreground",
                  disabled && "opacity-30 cursor-not-allowed text-muted-foreground",
                )}
              >
                <Icon className="w-4 h-4 md:w-3.5 md:h-3.5" />
              </button>
            );
          })}
        </div>
      ))}
      <input
        ref={imageInputRef}
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml"
        onChange={onImagePicked}
        className="hidden"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Selection pill — appears on text selection, opens Ask Yorik panel
// ---------------------------------------------------------------------------

function SelectionPill({
  editor, role, toast, args, activeTemplate, senderName, senderBusiness,
}: {
  editor: Editor;
  role: string;
  toast: (text: string, kind?: ToastKind) => void;
  args: Record<string, unknown>;
  activeTemplate: ComposeTemplate | null;
  senderName: string;
  senderBusiness: string;
}) {
  const [askOpen, setAskOpen] = useState(false);

  return (
    <>
      <BubbleMenu
        editor={editor}
        shouldShow={({ editor: ed, from, to }: { editor: Editor; from: number; to: number }) =>
          to > from && !askOpen && ed.state.doc.textBetween(from, to, " ").trim().length >= 2
        }
      >
        <button
          onClick={() => setAskOpen(true)}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-rose-500 hover:bg-rose-600 text-white text-xs font-semibold shadow-lg transition"
        >
          <Wand2 className="w-3 h-3" /> Ask Yorik
        </button>
      </BubbleMenu>
      {/* Panel lives OUTSIDE the BubbleMenu via a portal so the
          suggestions list isn't clipped by the editor's bounds
          (which happens when the selection is near the bottom/right
          edge, or when suggestions push the panel past the editor
          frame). Modal sits over the editor with a backdrop —
          matches the DebugBundleModal pattern. */}
      {askOpen && createPortal(
        <AskYorikPanel
          editor={editor}
          role={role}
          onClose={() => setAskOpen(false)}
          toast={toast}
          args={args}
          activeTemplate={activeTemplate}
          senderName={senderName}
          senderBusiness={senderBusiness}
        />,
        document.body,
      )}
    </>
  );
}

// Persisted in localStorage so the user's preferred verbosity sticks
// across sessions instead of always defaulting to "short".
type RevLength = "short" | "medium" | "long";
const REV_LEN_KEY = "yorik.revise.length";

function loadRevLength(): RevLength {
  if (typeof window === "undefined") return "medium";
  const v = window.localStorage.getItem(REV_LEN_KEY);
  return v === "short" || v === "medium" || v === "long" ? v : "medium";
}

// Heuristic: "does the selected text look like a template placeholder
// stub rather than real user content?" The two templates that ship today
// (generic-letter, generic-email) render parens-wrapped italic prose
// like "(Hier kommt dein Brieftext rein …)" when their body_text arg
// is empty. When the user selects that stub and asks Yorik to write
// something, the revise flow used to feed the stub to the LLM as
// context — the LLM tried to "preserve" the stub's meaning and
// produced garbled output. Detecting placeholder-shape selections
// switches the panel to a "write from scratch" mode that tells the
// backend to ignore the selection entirely.
//
// Tier-2 follow-up would be a structured marker (data-yorik-placeholder
// on the template <em> wrapper) — until then this heuristic catches
// the cases we actually ship.
function looksLikePlaceholder(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed) return false;
  if (trimmed.length > 250) return false;     // real prose is longer
  if (/^\(.*\)$/s.test(trimmed)) return true; // wrapped in parens
  if (/hier kommt (dein|der|die) /i.test(trimmed)) return true;
  if (/pick a template/i.test(trimmed)) return true;
  if (/lorem ipsum/i.test(trimmed)) return true;
  if (/\b(platzhalter|placeholder)\b/i.test(trimmed)) return true;
  return false;
}

// Pull the most useful facts out of the template's args + the user
// profile so the LLM can fill real names instead of writing [Name] /
// [Recipient] placeholders. Falls back gracefully when nothing is set.
function buildContextFacts(args: Record<string, unknown>,
                            template: ComposeTemplate | null,
                            senderName: string,
                            senderBusiness: string): Record<string, unknown> {
  function pick(...keys: string[]): string {
    for (const k of keys) {
      const v = args[k];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
    return "";
  }
  const out: Record<string, unknown> = {};
  const recipient = pick(
    "empfaenger_name", "recipient_name", "recipient",
    "vermieter_name", "anbieter_name", "locatore_nome",
    "najemca_imie_nazwisko",
  );
  const recipientAddr = pick(
    "empfaenger_adresse", "recipient_address", "recipient_address_line1",
    "vermieter_adresse", "anbieter_adresse", "locatore_indirizzo",
  );
  const subject = pick("betreff", "subject");
  if (recipient)     out.recipient_name    = recipient;
  if (recipientAddr) out.recipient_address = recipientAddr;
  if (subject)       out.subject           = subject;
  if (senderName)    out.sender_name       = senderName;
  if (senderBusiness) out.sender_business  = senderBusiness;
  if (template?.id)  out.kind              = (template.tags || []).includes("email") ? "email" : "letter";
  return out;
}

function AskYorikPanel({
  editor, role, onClose, toast, args, activeTemplate, senderName, senderBusiness,
}: {
  editor: Editor;
  role: string;
  onClose: () => void;
  toast: (text: string, kind?: ToastKind) => void;
  args: Record<string, unknown>;
  activeTemplate: ComposeTemplate | null;
  senderName: string;
  senderBusiness: string;
}) {
  const [instruction, setInstruction] = useState("");
  const [length, setLength] = useState<RevLength>(() => loadRevLength());
  useEffect(() => {
    if (typeof window !== "undefined") window.localStorage.setItem(REV_LEN_KEY, length);
  }, [length]);
  const [loading, setLoading] = useState(false);
  const [suggestions, setSuggestions] = useState<ComposeReviseSuggestion[]>([]);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Voice-input state. Whisper is per-file (no true streaming),
  // so the UX is: tap mic → record → tap to stop → "Transcribing…"
  // → final transcript drops into the textarea. Not real word-by-
  // word streaming, but it gets the user's words into the input
  // without typing.
  const [recState, setRecState] = useState<"idle" | "recording" | "transcribing">("idle");
  const recorderRef = useRef<MediaRecorder | null>(null);
  const recStreamRef = useRef<MediaStream | null>(null);
  const recChunksRef = useRef<Blob[]>([]);

  const { from, to } = editor.state.selection;
  const selectedText = editor.state.doc.textBetween(from, to, " ");
  // When the selection looks like a template stub, the panel switches
  // to "write fresh content" mode — header, placeholder, and the
  // request's `mode` field all change so the backend's prompt steers
  // the LLM to ignore the selection.
  const isPlaceholder = looksLikePlaceholder(selectedText);
  const mode: "revise" | "write" = isPlaceholder ? "write" : "revise";

  useEffect(() => {
    setTimeout(() => inputRef.current?.focus(), 30);
    function esc(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  // Auto-grow the textarea up to ~6 lines as the user types/dictates.
  // Without this the input stays a fixed single line and long
  // instructions scroll horizontally — invisible past the right edge.
  useEffect(() => {
    const ta = inputRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const maxPx = 6 * 20; // ~6 lines at 20px line-height
    ta.style.height = Math.min(ta.scrollHeight, maxPx) + "px";
  }, [instruction]);

  // ── Voice input via Whisper ───────────────────────────────────────
  // Stop any in-flight recording when the panel closes / unmounts so
  // the mic light doesn't stay on.
  useEffect(() => () => {
    if (recorderRef.current && recorderRef.current.state === "recording") {
      try { recorderRef.current.stop(); } catch { /* noop */ }
    }
    recStreamRef.current?.getTracks().forEach(t => t.stop());
  }, []);

  async function startRecording() {
    if (recState !== "idle") return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      toast("Browser doesn't support audio recording", "error");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recStreamRef.current = stream;
      const rec = new MediaRecorder(stream);
      recorderRef.current = rec;
      recChunksRef.current = [];
      rec.ondataavailable = (e) => { if (e.data && e.data.size) recChunksRef.current.push(e.data); };
      rec.onstop = async () => {
        // Release the mic immediately so the browser's red dot drops.
        recStreamRef.current?.getTracks().forEach(t => t.stop());
        recStreamRef.current = null;
        const blob = new Blob(recChunksRef.current, { type: rec.mimeType || "audio/webm" });
        recChunksRef.current = [];
        if (blob.size === 0) {
          setRecState("idle");
          toast("Recording was empty", "info");
          return;
        }
        setRecState("transcribing");
        try {
          const fd = new FormData();
          fd.append("audio", blob, `instruction.${(rec.mimeType || "audio/webm").split("/")[1].split(";")[0]}`);
          const r = await fetch("/api/voice/transcribe", {
            method: "POST", body: fd, credentials: "include",
          });
          if (!r.ok) {
            const txt = await r.text().catch(() => "");
            throw new Error(txt || `HTTP ${r.status}`);
          }
          const j = await r.json() as { text: string; language?: string };
          // Append (don't replace) so typed-and-then-spoken still works.
          // Trim leading whitespace so the join is clean.
          setInstruction(prev => {
            const sep = prev.trim() ? (prev.endsWith(" ") ? "" : " ") : "";
            return prev + sep + j.text.trim();
          });
          // Refocus so Enter sends without an extra click.
          setTimeout(() => inputRef.current?.focus(), 30);
        } catch (e: any) {
          toast(`Transcribe failed: ${e?.message || e}`, "error");
        } finally {
          setRecState("idle");
        }
      };
      rec.start();
      setRecState("recording");
    } catch (e: any) {
      toast(`Mic access denied: ${e?.message || e}`, "error");
      setRecState("idle");
    }
  }

  function stopRecording() {
    const rec = recorderRef.current;
    if (rec && rec.state === "recording") {
      try { rec.stop(); } catch { /* noop */ }
    }
  }

  async function fire() {
    const text = instruction.trim();
    if (!text) return;
    setLoading(true);
    setSuggestions([]);
    const before = editor.state.doc.textBetween(Math.max(0, from - 200), from, " ");
    const after  = editor.state.doc.textBetween(to, Math.min(editor.state.doc.content.size, to + 200), " ");
    const contextFacts = buildContextFacts(args, activeTemplate, senderName, senderBusiness);
    try {
      const r = await api.post<ComposeReviseResponse>(
        `/api/connectors/compose/invoke?role=${encodeURIComponent(role)}&layout_id=__system__`,
        { params: {
          op: "revise",
          selected_text: selectedText,
          instruction: text,
          context_before: before,
          context_after: after,
          length,
          context_facts: contextFacts,
          mode,
        }},
      );
      if (r.llm_offline) {
        toast(r.error || "Yorik's brain is offline — start the local LLM and retry", "error");
      } else if (!r.suggestions?.length) {
        toast("No suggestions came back — try rephrasing", "info");
      }
      setSuggestions(r.suggestions || []);
    } catch (e: any) {
      toast(`Ask Yorik failed: ${e.message}`, "error");
    } finally {
      setLoading(false);
    }
  }

  function accept(s: ComposeReviseSuggestion) {
    editor.chain().focus().insertContentAt({ from, to }, s.text).run();
    onClose();
  }

  return (
    <div
      className="fixed inset-0 z-[1200] bg-black/50 flex items-start justify-center p-4 pt-[10vh]"
      onClick={onClose}
    >
      <div
        className="bg-card text-card-foreground border border-border rounded-xl shadow-2xl w-full max-w-xl max-h-[80vh] flex flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
        onMouseDown={e => e.stopPropagation()}
      >
        <header className="px-4 py-2.5 border-b border-border flex items-center justify-between shrink-0">
          <div className="text-xs font-semibold flex items-center gap-1.5">
            <Wand2 className="w-3.5 h-3.5 text-rose-500" />
            {mode === "write" ? "Write text" : "Revise selection"}
          </div>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground transition">
            <X className="w-3.5 h-3.5" />
          </button>
        </header>
        <div className="px-4 py-3 text-[11px] text-muted-foreground italic line-clamp-2 bg-muted/30 shrink-0">
          {mode === "write"
            ? <>Template placeholder — will be ignored. Tell Yorik below what you want to write.</>
            : <>"{selectedText.slice(0, 200)}{selectedText.length > 200 ? "…" : ""}"</>}
        </div>
        <div className="px-3 pt-3 flex items-center gap-1 text-[10px] text-muted-foreground border-t border-border shrink-0">
          <span className="mr-1 uppercase tracking-wider">Length</span>
          {(["short", "medium", "long"] as RevLength[]).map(opt => (
            <button
              key={opt}
              type="button"
              onClick={() => setLength(opt)}
              className={cn(
                "px-2 py-0.5 rounded-md transition",
                length === opt
                  ? "bg-rose-500/15 text-rose-600 font-medium"
                  : "text-muted-foreground hover:text-foreground",
              )}
              title={
                opt === "short" ? "1-3 short alternatives (~1 sentence each)" :
                opt === "medium" ? "1-3 alternatives, 2-4 sentences each" :
                "ONE fully written 1-3 paragraph version"
              }
            >
              {opt === "short" ? "Short" : opt === "medium" ? "Medium" : "Long"}
            </button>
          ))}
        </div>
        <div className="p-3 flex gap-2 shrink-0 items-end">
          <textarea
            ref={inputRef}
            value={instruction}
            onChange={e => setInstruction(e.target.value)}
            onKeyDown={e => {
              // Enter submits; Shift+Enter inserts a newline so users
              // can still write multi-line instructions when they want to.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                fire();
              }
            }}
            rows={1}
            placeholder={
              recState === "recording"   ? "Recording — tap the microphone again to stop…" :
              recState === "transcribing" ? "Transcribing…" :
              mode === "write"
                ? "e.g. \"Thank-you note for the car detailing, 2 paragraphs, informal\""
                : "e.g. make it more formal, in German, expand into 2 paragraphs"
            }
            disabled={recState !== "idle"}
            className={cn(
              "flex-1 min-h-9 px-3 py-1.5 rounded-md bg-muted/60 text-sm leading-5 resize-none overflow-y-auto",
              "focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition",
              recState !== "idle" && "opacity-75",
            )}
          />
          <button
            onClick={recState === "recording" ? stopRecording : startRecording}
            disabled={recState === "transcribing" || loading}
            title={
              recState === "recording"   ? "Stop recording" :
              recState === "transcribing" ? "Transcribing…" :
              "Speak instruction"
            }
            className={cn(
              "w-9 h-9 rounded-md transition inline-flex items-center justify-center shrink-0",
              recState === "recording"
                ? "bg-rose-500 text-white hover:bg-rose-600 animate-pulse"
                : recState === "transcribing"
                  ? "bg-muted text-muted-foreground cursor-wait"
                  : "bg-muted/60 text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            {recState === "recording"   ? <Square className="w-3.5 h-3.5" />     :
             recState === "transcribing" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> :
                                           <Mic className="w-3.5 h-3.5" />}
          </button>
          <button
            onClick={fire}
            disabled={!instruction.trim() || loading || recState !== "idle"}
            className={cn(
              "px-3 h-9 rounded-md text-sm font-medium transition inline-flex items-center gap-1 shrink-0",
              instruction.trim() && !loading && recState === "idle"
                ? "bg-rose-500 text-white hover:bg-rose-600"
                : "bg-muted text-muted-foreground cursor-not-allowed",
            )}
          >
            {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "↵"}
          </button>
        </div>
        {suggestions.length > 0 && (
          <div className="border-t border-border flex-1 min-h-0 overflow-y-auto">
            {suggestions.map((s, i) => (
              <div key={i} className="p-3 border-b border-border/60 last:border-0">
                <div className="text-sm leading-relaxed mb-2 whitespace-pre-wrap">{s.text}</div>
                {s.rationale && (
                  <div className="text-[10px] text-muted-foreground italic mb-2">{s.rationale}</div>
                )}
                <button
                  onClick={() => accept(s)}
                  className="text-xs px-2.5 py-1 rounded-md bg-rose-500 hover:bg-rose-600 text-white font-medium transition"
                >
                  Accept
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Write-arg modal — structural alternative to selection-based revise.
//
// The user clicks "AI" on a specific template arg (body_text, notes, ...).
// We ask the backend to generate ONLY that field's content — no selection,
// no surrounding-context guessing, no "did the LLM treat the stub as
// real prose?" ambiguity. The response writes directly into args[key];
// the existing auto-rerender hook picks it up and renders the body.
//
// Duplicates ~80 lines of input + length + mic logic from AskYorikPanel.
// Shared "InstructionInputArea" component is a clean Tier-2 refactor;
// for first ship the duplication makes both code paths easy to read.
// ---------------------------------------------------------------------------

function WriteArgModal({
  argKey, argLabel, argRole, currentValue, role, toast, args, activeTemplate,
  senderName, senderBusiness, onClose, onAccept,
}: {
  argKey: string;
  argLabel: string;
  /** Declared template role (body / subject / etc.) — passed through to
   *  the backend so it can pick the right field-shape prompt rule. */
  argRole: string;
  currentValue: string;
  role: string;
  toast: (text: string, kind?: ToastKind) => void;
  args: Record<string, unknown>;
  activeTemplate: ComposeTemplate | null;
  senderName: string;
  senderBusiness: string;
  onClose: () => void;
  onAccept: (text: string) => void;
}) {
  const [instruction, setInstruction] = useState("");
  const [length, setLength] = useState<RevLength>(() => loadRevLength());
  useEffect(() => {
    if (typeof window !== "undefined") window.localStorage.setItem(REV_LEN_KEY, length);
  }, [length]);
  const [loading, setLoading] = useState(false);
  const [suggestion, setSuggestion] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [recState, setRecState] = useState<"idle" | "recording" | "transcribing">("idle");
  const recorderRef = useRef<MediaRecorder | null>(null);
  const recStreamRef = useRef<MediaStream | null>(null);
  const recChunksRef = useRef<Blob[]>([]);

  useEffect(() => {
    setTimeout(() => inputRef.current?.focus(), 30);
    function esc(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  // Auto-grow the instruction textarea up to ~6 lines.
  useEffect(() => {
    const ta = inputRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 6 * 20) + "px";
  }, [instruction]);

  // Stop in-flight recording on close.
  useEffect(() => () => {
    if (recorderRef.current && recorderRef.current.state === "recording") {
      try { recorderRef.current.stop(); } catch { /* noop */ }
    }
    recStreamRef.current?.getTracks().forEach(t => t.stop());
  }, []);

  async function startRecording() {
    if (recState !== "idle") return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      toast("Browser doesn't support audio recording", "error");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recStreamRef.current = stream;
      const rec = new MediaRecorder(stream);
      recorderRef.current = rec;
      recChunksRef.current = [];
      rec.ondataavailable = (e) => { if (e.data && e.data.size) recChunksRef.current.push(e.data); };
      rec.onstop = async () => {
        recStreamRef.current?.getTracks().forEach(t => t.stop());
        recStreamRef.current = null;
        const blob = new Blob(recChunksRef.current, { type: rec.mimeType || "audio/webm" });
        recChunksRef.current = [];
        if (blob.size === 0) { setRecState("idle"); toast("Recording was empty", "info"); return; }
        setRecState("transcribing");
        try {
          const fd = new FormData();
          fd.append("audio", blob, `instruction.${(rec.mimeType || "audio/webm").split("/")[1].split(";")[0]}`);
          const r = await fetch("/api/voice/transcribe", { method: "POST", body: fd, credentials: "include" });
          if (!r.ok) {
            const txt = await r.text().catch(() => "");
            throw new Error(txt || `HTTP ${r.status}`);
          }
          const j = await r.json() as { text: string };
          setInstruction(prev => {
            const sep = prev.trim() ? (prev.endsWith(" ") ? "" : " ") : "";
            return prev + sep + j.text.trim();
          });
          setTimeout(() => inputRef.current?.focus(), 30);
        } catch (e: any) {
          toast(`Transcribe failed: ${e?.message || e}`, "error");
        } finally {
          setRecState("idle");
        }
      };
      rec.start();
      setRecState("recording");
    } catch (e: any) {
      toast(`Mic access denied: ${e?.message || e}`, "error");
      setRecState("idle");
    }
  }
  function stopRecording() {
    const rec = recorderRef.current;
    if (rec && rec.state === "recording") { try { rec.stop(); } catch { /* noop */ } }
  }

  async function fire() {
    const text = instruction.trim();
    if (!text) return;
    setLoading(true);
    setSuggestion(null);
    const contextFacts = buildContextFacts(args, activeTemplate, senderName, senderBusiness);
    try {
      const r = await api.post<ComposeReviseResponse>(
        `/api/connectors/compose/invoke?role=${encodeURIComponent(role)}&layout_id=__system__`,
        { params: {
          op: "write_arg",
          target_arg_key: argKey,
          arg_label: argLabel,
          target_arg_role: argRole,
          current_value: currentValue,
          instruction: text,
          length,
          context_facts: contextFacts,
        }},
      );
      if (r.llm_offline) {
        toast(r.error || "Yorik's brain is offline — start the local LLM and retry", "error");
      } else if (!r.suggestions?.length) {
        toast("No content came back — try rephrasing", "info");
      } else {
        setSuggestion(r.suggestions[0].text);
      }
    } catch (e: any) {
      toast(`AI write failed: ${e.message}`, "error");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[1200] bg-black/50 flex items-start justify-center p-4 pt-[10vh]"
      onClick={onClose}
    >
      <div
        className="bg-card text-card-foreground border border-border rounded-xl shadow-2xl w-full max-w-xl max-h-[80vh] flex flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-4 py-2.5 border-b border-border flex items-center justify-between shrink-0">
          <div className="text-xs font-semibold flex items-center gap-1.5">
            <Sparkles className="w-3.5 h-3.5 text-violet-500" />
            Write {argLabel} with AI
          </div>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground transition">
            <X className="w-3.5 h-3.5" />
          </button>
        </header>
        {currentValue.trim() && (
          <div className="px-4 py-3 text-[11px] text-muted-foreground italic line-clamp-2 bg-muted/30 shrink-0">
            Current: "{currentValue.slice(0, 200)}{currentValue.length > 200 ? "…" : ""}" — will be replaced.
          </div>
        )}
        <div className="px-3 pt-3 flex items-center gap-1 text-[10px] text-muted-foreground border-t border-border shrink-0">
          <span className="mr-1 uppercase tracking-wider">Length</span>
          {(["short", "medium", "long"] as RevLength[]).map(opt => (
            <button
              key={opt}
              type="button"
              onClick={() => setLength(opt)}
              className={cn(
                "px-2 py-0.5 rounded-md transition",
                length === opt
                  ? "bg-violet-500/15 text-violet-600 font-medium"
                  : "text-muted-foreground hover:text-foreground",
              )}
              title={
                opt === "short" ? "1-2 sentences" :
                opt === "medium" ? "2-4 sentences" :
                "1-3 paragraphs"
              }
            >
              {opt === "short" ? "Short" : opt === "medium" ? "Medium" : "Long"}
            </button>
          ))}
        </div>
        <div className="p-3 flex gap-2 shrink-0 items-end">
          <textarea
            ref={inputRef}
            value={instruction}
            onChange={e => setInstruction(e.target.value)}
            onKeyDown={e => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); fire(); }
            }}
            rows={1}
            placeholder={
              recState === "recording"   ? "Recording — tap the microphone again to stop…" :
              recState === "transcribing" ? "Transcribing…" :
              `e.g. "Thank-you note for the detailing at Max's, informal"`
            }
            disabled={recState !== "idle"}
            className={cn(
              "flex-1 min-h-9 px-3 py-1.5 rounded-md bg-muted/60 text-sm leading-5 resize-none overflow-y-auto",
              "focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition",
              recState !== "idle" && "opacity-75",
            )}
          />
          <button
            onClick={recState === "recording" ? stopRecording : startRecording}
            disabled={recState === "transcribing" || loading}
            title={
              recState === "recording"   ? "Stop recording" :
              recState === "transcribing" ? "Transcribing…" :
              "Speak instruction"
            }
            className={cn(
              "w-9 h-9 rounded-md transition inline-flex items-center justify-center shrink-0",
              recState === "recording"
                ? "bg-rose-500 text-white hover:bg-rose-600 animate-pulse"
                : recState === "transcribing"
                  ? "bg-muted text-muted-foreground cursor-wait"
                  : "bg-muted/60 text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            {recState === "recording"   ? <Square className="w-3.5 h-3.5" /> :
             recState === "transcribing" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> :
                                           <Mic className="w-3.5 h-3.5" />}
          </button>
          <button
            onClick={fire}
            disabled={!instruction.trim() || loading || recState !== "idle"}
            className={cn(
              "px-3 h-9 rounded-md text-sm font-medium transition inline-flex items-center gap-1 shrink-0",
              instruction.trim() && !loading && recState === "idle"
                ? "bg-violet-500 text-white hover:bg-violet-600"
                : "bg-muted text-muted-foreground cursor-not-allowed",
            )}
          >
            {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "↵"}
          </button>
        </div>
        {suggestion && (
          <div className="border-t border-border flex-1 min-h-0 overflow-y-auto">
            <div className="p-3">
              <div className="text-sm leading-relaxed mb-3 whitespace-pre-wrap">{suggestion}</div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => onAccept(suggestion)}
                  className="text-xs px-3 py-1.5 rounded-md bg-violet-500 hover:bg-violet-600 text-white font-medium transition"
                >
                  Apply
                </button>
                <button
                  onClick={() => { setSuggestion(null); setTimeout(() => inputRef.current?.focus(), 30); }}
                  className="text-xs px-3 py-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition"
                >
                  Discard
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AI source-data pane (right) — args editor + pulled-data view
// ---------------------------------------------------------------------------

// Same convention map as backend/compose/series.py ARG_KEY_TO_KIND — used to
// surface "this looks like an invoice number, set up a series" hints even
// before any series exists. Keep in sync if the backend list grows.
const NUMBERING_HINTS: Record<string, string> = {
  rechnungsnummer: "rechnung", rechnungs_nummer: "rechnung", rechnung_nr: "rechnung",
  angebotsnummer: "angebot",   angebots_nummer: "angebot",   angebot_nr: "angebot",
  gutschriftsnummer: "gutschrift", gutschrift_nr: "gutschrift",
  mahnungsnummer: "mahnung",   mahnung_nr: "mahnung",
  invoice_number: "invoice", invoice_no: "invoice", inv_no: "invoice", invoice_id: "invoice",
  quote_number: "quote",     quote_no: "quote",     estimate_number: "quote",
  receipt_number: "receipt", receipt_no: "receipt",
  credit_note_number: "credit_note",
  faktura_nr: "faktura", numer_faktury: "faktura",
};

function inferKind(argKey: string): string | undefined {
  return NUMBERING_HINTS[(argKey || "").toLowerCase().replace(/-/g, "_")];
}

function AiPane({
  template, args, pulledData, numbering, onArgsChange, onRerender, onScheduleRerender, drafting,
  onOpenNumbering, onContactPicked, role, toast, senderName, senderBusiness,
}: {
  template: ComposeTemplate;
  args: Record<string, unknown>;
  pulledData: Record<string, unknown> | null;
  numbering: Record<string, NumberingMatch>;
  onArgsChange: (a: Record<string, unknown>) => void;
  // Optional args override lets callers (e.g. ExtractFromTextPanel after
  // applyValues) trigger the rerender with the freshly-set args BEFORE
  // React has applied the setState — without it the parent's `rerender`
  // captures stale args from closure and the server round-trip wipes
  // the just-set values.
  onRerender: (argsOverride?: Record<string, unknown>) => void;
  // Debounced auto-rerender — typing in any ArgInput should reflect
  // in the editor without forcing the user to click "Re-render".
  // Wired only from the user-typing path (setArg) so imperative
  // setArgs callers don't loop with their own server roundtrips.
  onScheduleRerender?: () => void;
  drafting: boolean;
  onOpenNumbering: (kind?: string) => void;
  onContactPicked?: (r: RecipientFillResult) => void;
  // Below: needed for the per-arg "Write with AI" modal — sends the
  // user's instruction + template context to /api/connectors/compose
  // op=write_arg and writes the response straight into the arg.
  role: string;
  toast: (text: string, kind?: ToastKind) => void;
  senderName: string;
  senderBusiness: string;
}) {
  function setArg(k: string, v: unknown) {
    onArgsChange({ ...args, [k]: v });
    onScheduleRerender?.();
  }
  // Which arg's "Write with AI" modal is open. null = closed. Modal
  // lives at this level so the per-row ArgInput stays simple and so
  // the modal portals cleanly to document.body.
  const [writeArgFor, setWriteArgFor] = useState<string | null>(null);
  // The field schema lookup (label + input shape + declarative role)
  // for the modal header and for "should this arg show an AI button?"
  // decisions. `role` is the canonical signal (set in the template
  // JSON — see templates/SCHEMA.md); `input` and key-name regex are
  // fallbacks for templates that haven't been migrated yet.
  const fieldSchema = useMemo(() => {
    const out: Record<string, {
      label: string;
      input?: string;
      role?: string;
      contact_group?: string;
      required?: boolean;
      hidden_when_positions_set?: boolean;
      // Nested schema for role=line_items entries — each row of the
      // dynamic list editor is rendered from these per-field specs.
      item_schema?: Array<{
        key: string;
        label?: string;
        type?: string;
        required?: boolean;
        default?: unknown;
        hint?: string;
      }>;
      min_items?: number;
      hint?: string;
    }> = {};
    for (const f of (template.ask_user_for_args || [])) {
      out[f.key] = {
        label: f.label || f.key,
        input: f.input,
        role: f.role,
        // contact_group lets a template declare "this name+address pair
        // is the Arbeitgeber, that pair is the Verwalter" so picking a
        // contact for one slot doesn't bleed into the other. See
        // templates/SCHEMA.md.
        contact_group: (f as any).contact_group,
        // Required flag drives the three-tier args panel layout
        // (required-in-body → optional-in-body → envelope/metadata) and
        // the inline red asterisk on each row. The "X/Y required filled"
        // header counter already uses `f.required` directly from the
        // template — surfacing it per-row gives the user a visible signal
        // wherever they're scanning instead of forcing them to count.
        required: !!f.required,
        // Sub-round 2 (positions[]). When the args panel is editing
        // positions[] via the LineItemsEditor, the legacy
        // position_<N>_<suffix> rows are hidden — same data exposed
        // through one shape avoids duplicate-edit confusion. The
        // template tags those legacy entries with this flag.
        hidden_when_positions_set: !!(f as any).hidden_when_positions_set,
        item_schema: (f as any).item_schema,
        min_items: (f as any).min_items,
        hint: (f as any).hint,
      };
    }
    return out;
  }, [template]);

  // Given a name-arg key, find its matching address-arg key. Role +
  // contact_group declarations win; falls back to prefix detection so
  // legacy templates (no ask_user_for_args, or no role declarations)
  // keep working unchanged.
  const addressKeyFor = useCallback((nameKey: string): string | null => {
    const nameSchema = fieldSchema[nameKey];
    if (nameSchema?.role === "recipient_name") {
      const group = nameSchema.contact_group || null;
      for (const [k, s] of Object.entries(fieldSchema)) {
        if (s?.role !== "recipient_address") continue;
        if (group !== null && (s.contact_group || null) !== group) continue;
        return k;
      }
      // Role declared but no matching address arg in same group —
      // don't fall through to prefix (template likely intentional).
      return null;
    }
    return findAddressKeyForName(nameKey, Object.keys(fieldSchema).length
      ? Object.keys(fieldSchema)
      : Object.keys(args || {}));
  }, [fieldSchema, args]);
  function isWritableTextArg(k: string, v: unknown): boolean {
    const schema = fieldSchema[k];
    // Canonical: declared role wins
    if (schema?.role === "body") return true;
    if (schema?.role && schema.role !== "freeform_text") {
      // A non-body declared role means "this is NOT a body field" —
      // respect that and don't fall through to the regex fallback
      // (which would mis-fire on names like `mangel_kurz`).
      return false;
    }
    // Template hint
    if (schema?.input === "textarea") return true;
    // Key-name fallback for un-migrated templates
    if (/(^|_)(body|text|content|message|notes|brief)(_|$)/i.test(k)) return true;
    // Current value already multi-line / long
    if (typeof v === "string" && (v.includes("\n") || v.length > 80)) return true;
    return false;
  }
  // Subject-shape fields get a different AI affordance: one click,
  // no instruction needed, LLM auto-writes from the document body.
  function isAutoSubjectArg(k: string): boolean {
    const schema = fieldSchema[k];
    if (schema?.role === "subject") return true;
    if (schema?.role && schema.role !== "freeform_value") return false;
    // Fallback: strict closed set on key name (no false-positive risk
    // because anything matching is unambiguously a subject field).
    return /^(betreff|subject|title)$/i.test(k);
  }
  // Pull whatever body-shape arg exists from the current args dict
  // so the auto-subject call can see what the document is about.
  // Falls back to "" when no body is filled — the LLM then has to
  // work from CONTEXT FACTS alone (recipient + kind).
  function getDocumentBody(): string {
    for (const candidate of ["body_text", "body", "text", "content", "message", "notes"]) {
      const v = args[candidate];
      if (typeof v === "string" && v.trim()) return v;
    }
    // Last resort: any string arg longer than 80 chars
    for (const k of Object.keys(args)) {
      const v = args[k];
      if (typeof v === "string" && v.trim().length > 80) return v;
    }
    return "";
  }
  // One-click auto-generate for subject-shape fields. No modal —
  // straight to the LLM with the body as context. Pending-state
  // shows a spinner on the button.
  const [autoGenPending, setAutoGenPending] = useState<string | null>(null);
  async function autoGenerateArg(k: string) {
    if (autoGenPending) return;
    setAutoGenPending(k);
    const contextFacts = buildContextFacts(args, template, senderName, senderBusiness);
    try {
      const r = await api.post<ComposeReviseResponse>(
        `/api/connectors/compose/invoke?role=${encodeURIComponent(role)}&layout_id=__system__`,
        { params: {
          op: "write_arg",
          target_arg_key: k,
          arg_label: fieldSchema[k]?.label || k,
          target_arg_role: fieldSchema[k]?.role || "",
          current_value: typeof args[k] === "string" ? args[k] as string : "",
          instruction: "",         // empty -> backend uses field-shape default
          length: "short",         // subjects are one line
          context_facts: contextFacts,
          document_body: getDocumentBody(),
        }},
      );
      if (r.llm_offline) {
        toast(r.error || "Yorik's brain is offline — start the local LLM and retry", "error");
      } else if (!r.suggestions?.length) {
        toast("No subject came back — try writing the body first", "info");
      } else {
        // Subjects sometimes come back wrapped in quotes from the LLM;
        // strip a single leading/trailing pair so they don't end up in
        // the rendered output.
        let subject = r.suggestions[0].text.trim();
        if ((subject.startsWith('"') && subject.endsWith('"'))
         || (subject.startsWith("„") && subject.endsWith("\""))) {
          subject = subject.slice(1, -1);
        }
        onArgsChange({ ...args, [k]: subject });
        onScheduleRerender?.();
      }
    } catch (e: any) {
      toast(`Auto-subject failed: ${e?.message || e}`, "error");
    } finally {
      setAutoGenPending(null);
    }
  }

  const argKeys = Object.keys(args || {});
  const dataKeys = pulledData ? Object.keys(pulledData) : [];

  // Args that LOOK like document numbers but have no series wired up yet —
  // these get the "🛠 Set up [kind] numbering" CTA so the user discovers the
  // feature exactly when it's useful.
  const unmatchedNumberingArgs = argKeys
    .map(k => ({ key: k, kind: inferKind(k), matched: !!numbering[k] }))
    .filter(x => x.kind && !x.matched);

  return (
    <>
      <header className="h-16 px-5 flex items-center gap-2 border-b border-border">
        <div className="w-8 h-8 rounded-full bg-violet-500/15 flex items-center justify-center">
          <Sparkles className="w-4 h-4 text-violet-500" />
        </div>
        <div className="min-w-0">
          <div className="font-semibold leading-none text-sm truncate">{template.name}</div>
          <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5">
            Template · arguments · data
          </div>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-5 space-y-5">
        {template.description && (
          <p className="text-xs text-muted-foreground leading-relaxed">{template.description}</p>
        )}

        {unmatchedNumberingArgs.length > 0 && (
          <section>
            <h4 className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
              Numbering setup
            </h4>
            <div className="space-y-2">
              {unmatchedNumberingArgs.map(({ key, kind }) => (
                <button
                  key={key}
                  onClick={() => onOpenNumbering(kind)}
                  className="w-full text-left bg-amber-500/10 border border-amber-500/30 rounded-lg p-2.5 hover:bg-amber-500/15 transition group"
                >
                  <div className="flex items-center gap-2 text-xs">
                    <Hash className="w-3.5 h-3.5 text-amber-600" />
                    <span className="font-medium text-foreground">Set up <code className="bg-card/60 px-1 rounded">{kind}</code> numbering</span>
                    <span className="ml-auto text-[10px] text-amber-600 group-hover:underline">Configure →</span>
                  </div>
                  <div className="text-[11px] text-muted-foreground mt-1 leading-snug">
                    This template uses <code className="text-foreground/70">{key}</code> — Yorik can
                    auto-allocate sequential numbers and log them for tax-audit proof.
                  </div>
                </button>
              ))}
            </div>
          </section>
        )}

        {argKeys.length > 0 && (
          <section>
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">
                Arguments
              </h4>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => onOpenNumbering()}
                  className="text-[10px] inline-flex items-center gap-1 px-2 py-0.5 rounded bg-muted/40 hover:bg-muted/70 text-muted-foreground hover:text-foreground transition"
                  title="Manage document numbering"
                >
                  <Hash className="w-2.5 h-2.5" /> Numbering
                </button>
                <button
                  onClick={() => onRerender()}
                  disabled={drafting}
                  className="text-[10px] inline-flex items-center gap-1 px-2 py-0.5 rounded bg-violet-500/10 hover:bg-violet-500/20 text-violet-500 transition disabled:opacity-50"
                  title="Re-run the template with the current arguments"
                >
                  {drafting ? <Loader2 className="w-2.5 h-2.5 animate-spin" /> : <RefreshCw className="w-2.5 h-2.5" />}
                  Re-render
                </button>
              </div>
            </div>

            <ExtractFromTextPanel
              template={template}
              args={args}
              onArgsChange={onArgsChange}
              onRerender={onRerender}
            />

            <RecipientGroupsSection
              fieldSchema={fieldSchema}
              argKeys={argKeys}
              args={args}
              onArgsChange={(next) => {
                onArgsChange(next);
                onRerender?.(next);
              }}
              onContactPicked={onContactPicked}
            />

            <ArgsList
              argKeys={argKeys}
              args={args}
              numbering={numbering}
              setArg={setArg}
              isWritableTextArg={isWritableTextArg}
              isAutoSubjectArg={isAutoSubjectArg}
              autoGenPending={autoGenPending}
              setWriteArgFor={setWriteArgFor}
              autoGenerateArg={autoGenerateArg}
              bodyHtml={template.body_html || ""}
              fieldSchema={fieldSchema}
            />
          </section>
        )}
        {writeArgFor !== null && createPortal(
          <WriteArgModal
            argKey={writeArgFor}
            argLabel={fieldSchema[writeArgFor]?.label || writeArgFor}
            argRole={fieldSchema[writeArgFor]?.role || ""}
            currentValue={typeof args[writeArgFor] === "string" ? args[writeArgFor] as string : ""}
            role={role}
            toast={toast}
            args={args}
            activeTemplate={template}
            senderName={senderName}
            senderBusiness={senderBusiness}
            onClose={() => setWriteArgFor(null)}
            onAccept={(text) => {
              onArgsChange({ ...args, [writeArgFor]: text });
              onScheduleRerender?.();
              setWriteArgFor(null);
            }}
          />,
          document.body,
        )}

        {dataKeys.length > 0 && (
          <section>
            <h4 className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
              Pulled data
            </h4>
            <div className="space-y-1.5">
              {dataKeys.map(k => (
                <DataRow key={k} k={k} v={(pulledData as any)[k]} />
              ))}
            </div>
          </section>
        )}

        {template.requires_extensions?.length > 0 && (
          <section>
            <h4 className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
              Requires extensions
            </h4>
            <div className="flex flex-wrap gap-1">
              {template.requires_extensions.map(e => (
                <span key={e} className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-600 border border-amber-500/20">
                  {e}
                </span>
              ))}
            </div>
          </section>
        )}

        <div className="pt-4 mt-2 border-t border-border text-[11px] text-muted-foreground leading-relaxed">
          Highlight any text in the editor → an <strong className="text-foreground/80">Ask Yorik</strong> pill
          appears to revise that selection (formal, shorter, in German, etc).
        </div>
      </div>
    </>
  );
}

// "Fill from text" — paste any blob (email, note, contact card),
// LLM maps it onto the template's named arguments. Same backend as
// the chat's NeedsInputCard panel; reuses POST /api/compose/extract-fields.
// Empty-only fill by default — never overwrites user-typed values
// unless the explicit "Overwrite" link is clicked.
function ExtractFromTextPanel({
  template, args, onArgsChange, onRerender,
}: {
  template: ComposeTemplate;
  args: Record<string, unknown>;
  onArgsChange: (a: Record<string, unknown>) => void;
  /** Re-render the template after fields land — without this, the
   *  letter body in the editor stays stale and the user thinks
   *  nothing happened. argsOverride passes the freshly-set args
   *  directly so the server doesn't round-trip stale closure state. */
  onRerender?: (argsOverride?: Record<string, unknown>) => void;
}) {
  // Collapsed by default now — the dedicated RecipientGroupsSection
  // below covers the common case (contact → recipient fields). This
  // panel earns its keep for the less-common "fill EVERY field at once
  // from a pasted email/note" flow (subject, date, body references in
  // one shot), so we don't burn screen real-estate on it by default.
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Show what got filled SO the user has visible feedback instead of
  // the panel silently closing. Reset on each new run.
  const [lastFilled, setLastFilled] = useState<Array<{ key: string; value: string }>>([]);
  const [extractedOnce, setExtractedOnce] = useState(false);

  // Build the field schema from the template. Prefer ask_user_for_args
  // (carries labels + required flags) and fall back to the default_args
  // keys when an older template doesn't declare a schema.
  const fields = useMemo(() => {
    if (template.ask_user_for_args?.length) {
      return template.ask_user_for_args.map(f => ({
        key: f.key, label: f.label || f.key, pattern: f.pattern,
      }));
    }
    return Object.keys(template.default_args || {}).map(k => ({
      key: k, label: k,
    }));
  }, [template]);

  function applyValues(values: Record<string, string>, overwrite: boolean) {
    const filled: Array<{ key: string; value: string }> = [];
    const next = { ...args };
    for (const f of fields) {
      const v = values[f.key];
      if (!v) continue;
      const cur = String(next[f.key] ?? "").trim();
      if (cur && !overwrite) continue;
      next[f.key] = v;
      filled.push({ key: f.key, value: v });
    }
    onArgsChange(next);
    setLastFilled(filled);
    setExtractedOnce(true);
    // Re-render the template so the body in the editor REFLECTS the
    // new values. Pass `next` directly — the parent's rerender
    // captures `args` from closure which is still the PRE-update
    // value here, so a bare onRerender() would server-roundtrip
    // stale args and the response would wipe what we just filled.
    if (filled.length > 0) onRerender?.(next);
  }

  async function run(overwrite: boolean) {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{ values: Record<string, string> }>(
        "/api/compose/extract-fields",
        { text, fields },
      );
      applyValues(r.values || {}, overwrite);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  // applyContact() (regex-heuristic per-template contact mapping) lived
  // here. Removed 2026-06-02 once every recipient-bearing template got
  // declarative role + contact_group — the dedicated
  // RecipientGroupsSection (above the arg list) does this strictly
  // better: per-group, role-driven, fills more fields.

  return (
    <div className="mb-2 rounded-md border border-violet-500/15 bg-violet-500/[0.04]">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="w-full px-2.5 py-1.5 flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground transition"
      >
        <Wand2 className="w-3 h-3 text-violet-500" />
        <span className="font-medium">Paste entire text</span>
        <span className="text-muted-foreground/80">
          — paste an email or note, Yorik fills every matching field at once (subject, dates, amounts, body…)
        </span>
        <span className="ml-auto">
          {open
            ? <ChevronUp className="w-3 h-3" />
            : <ChevronDown className="w-3 h-3" />}
        </span>
      </button>
      {open && (
        <div className="px-2.5 pb-2.5 space-y-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="e.g. an email or note with names, address, date, amount…"
            rows={4}
            className="w-full text-xs bg-background border border-border rounded-md px-2 py-1.5 resize-y focus:outline-none focus:ring-1 focus:ring-ring/40"
          />
          <div className="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              onClick={() => run(false)}
              disabled={busy || !text.trim()}
              className="h-7 px-2.5 rounded-md bg-violet-500 text-white text-[11px] font-medium hover:opacity-90 disabled:opacity-50 flex items-center gap-1.5"
            >
              {busy
                ? <Loader2 className="w-3 h-3 animate-spin" />
                : <Sparkles className="w-3 h-3" />}
              Fill fields
            </button>
            {extractedOnce && (
              <button
                type="button"
                onClick={() => run(true)}
                disabled={busy || !text.trim()}
                className="h-7 px-2 rounded-md text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-50 flex items-center gap-1"
                title="Also overwrite fields that are already filled"
              >
                Overwrite
              </button>
            )}
            {extractedOnce && lastFilled.length === 0 && !busy && (
              <span className="text-[10px] text-amber-600">
                Nothing matched — the text may not contain any known fields.
              </span>
            )}
          </div>

          {/* What got filled — visible feedback so the user doesn't think
              "nothing happened". Also dismissable so re-runs feel fresh. */}
          {lastFilled.length > 0 && (
            <div className="rounded-md border border-emerald-500/30 bg-emerald-500/[0.06] p-2">
              <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-emerald-700 dark:text-emerald-400 font-semibold mb-1">
                <span className="flex items-center gap-1">
                  <Check className="w-2.5 h-2.5" />
                  {lastFilled.length} field{lastFilled.length === 1 ? "" : "s"} filled
                </span>
                <button
                  onClick={() => setLastFilled([])}
                  className="text-emerald-700/70 hover:text-emerald-700 dark:text-emerald-400/70 dark:hover:text-emerald-400"
                  title="Hide"
                >
                  <X className="w-2.5 h-2.5" />
                </button>
              </div>
              <ul className="text-[10px] space-y-0.5">
                {lastFilled.map(({ key, value }) => (
                  <li key={key} className="truncate">
                    <code className="opacity-70">{key}</code>:{" "}
                    <span className="text-foreground/85">{value.length > 60 ? value.slice(0, 60) + "…" : value}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {error && (
            <div className="text-[10px] text-rose-500">{error}</div>
          )}
        </div>
      )}
    </div>
  );
}


// Inline paste/upload affordance shown next to a contact picker. The
// user pastes free text (email signature, snipped letter header, copied
// document content) OR uploads a file; the LLM extracts only the
// fields listed in `targets` — so on a multi-recipient template, the
// Vermieter paste fills vermieter_name + vermieter_adresse without
// touching the Verwalter slot.
// ─── Recipient groups section ──────────────────────────────────────
// Shown above the per-arg list. Templates declare contact_group on each
// role-tagged arg ("vermieter", "mieter", etc.); this component renders
// one row per group with a single contact picker. Picking a contact
// fills every group-tagged arg via the role mapping in
// RecipientPicker.contactRoleValues — name, address, email, phone,
// iban, tax_id, etc. — wherever the template declares a slot for it.
// The per-arg inline picker is gone; arg rows are pre-filled and stay
// editable for one-off tweaks.

interface FieldSchemaEntry {
  label: string;
  input?: string;
  role?: string;
  contact_group?: string;
}

function RecipientGroupsSection({
  fieldSchema, argKeys, args, onArgsChange, onContactPicked,
}: {
  fieldSchema: Record<string, FieldSchemaEntry>;
  argKeys: string[];
  args: Record<string, unknown>;
  onArgsChange: (next: Record<string, unknown>) => void;
  onContactPicked?: (r: RecipientFillResult) => void;
}) {
  // Group the args by contact_group. Args with a role starting with
  // 'recipient_' that have NO group declared land in the anonymous
  // group (single-recipient templates). Args with no recipient role
  // are ignored — they don't belong to any picker.
  const groups: Array<{
    group: string | null;
    label: string;
    argKeys: string[];
  }> = useMemo(() => {
    const byGroup = new Map<string | null, string[]>();
    for (const k of argKeys) {
      const s = fieldSchema[k];
      if (!s?.role?.startsWith("recipient_")) continue;
      const g = s.contact_group || null;
      if (!byGroup.has(g)) byGroup.set(g, []);
      byGroup.get(g)!.push(k);
    }
    if (byGroup.size === 0) return [];
    const out: Array<{ group: string | null; label: string; argKeys: string[] }> = [];
    for (const [g, ks] of byGroup) {
      // Label: explicit group slug capitalised, or fall back to the
      // recipient_name arg's own label, or generic.
      let label = g
        ? g.charAt(0).toUpperCase() + g.slice(1)
        : "Recipient";
      if (!g) {
        // Anonymous group → use the recipient_name label if available
        const nk = ks.find(k => fieldSchema[k]?.role === "recipient_name");
        if (nk && fieldSchema[nk].label) label = fieldSchema[nk].label;
      }
      out.push({ group: g, label, argKeys: ks });
    }
    // Stable order: anonymous group first (single-recipient templates),
    // then declared groups in template-declaration order (Map preserves
    // insertion order).
    return out;
  }, [fieldSchema, argKeys]);

  if (groups.length === 0) return null;

  function fillGroup(groupKeys: string[], r: RecipientFillResult) {
    // For each arg in this group, look up its role and pull the matching
    // value from r.fillsByRole. Args whose role doesn't appear in the
    // mapping stay unchanged (the contact had no value for that field).
    //
    // Greeting/anrede is NOT touched here. Templates put the recipient
    // name in its own <span data-arg-key="empfaenger_name"> right next
    // to the <span data-arg-key="anrede"> prefix, so picking a contact
    // updates only the name span; the prefix stays whatever the user
    // typed. The previous heuristic that auto-swapped names inside the
    // greeting string is gone — same one-line render, simpler model.
    const next: Record<string, unknown> = { ...args };
    let filled = 0;
    for (const k of groupKeys) {
      const role = fieldSchema[k]?.role;
      if (!role) continue;
      const v = r.fillsByRole[role];
      if (v !== undefined) {
        next[k] = v;
        filled++;
      }
    }
    if (filled > 0) onArgsChange(next);
    onContactPicked?.(r);
  }

  return (
    <div className="rounded-2xl border border-amber-500/30 bg-amber-500/[0.04] p-4 space-y-3">
      <div className="flex items-center gap-2">
        <UsersRound className="w-4 h-4 text-amber-600" />
        <div className="text-[11px] uppercase tracking-wider font-semibold text-amber-700 dark:text-amber-400">
          Recipients
        </div>
      </div>
      <div className="space-y-2">
        {groups.map(({ group, label, argKeys: groupKeys }) => {
          // Compute paste-extract targets for this group: every arg in
          // the group, labelled by its field-schema label. Same targets
          // the inline GroupExtractor used to consume.
          const extractTargets = groupKeys.map(k => ({
            key: k,
            label: fieldSchema[k]?.label || k,
          }));
          // The legacy picker is anchored to a "name key" so its
          // address-sibling fallback still works for templates without
          // contact_group. Use the recipient_name arg of this group;
          // when absent, pick the first arg.
          const nameKey = groupKeys.find(k => fieldSchema[k]?.role === "recipient_name") || groupKeys[0];
          // Reflect the live state of this group so the section tracks
          // what the user typed/picked instead of staying frozen on a
          // static "Empfänger" header. Reads recipient-flavoured roles
          // out of the current args dict — anything filled here renders
          // an icon next to the name preview.
          const emailKey   = groupKeys.find(k => fieldSchema[k]?.role === "recipient_email");
          const phoneKey   = groupKeys.find(k => fieldSchema[k]?.role === "recipient_phone");
          const addressKey = groupKeys.find(k => fieldSchema[k]?.role === "recipient_address");
          const currentName = String(args[nameKey] ?? "").trim();
          const hasEmail   = !!emailKey   && String(args[emailKey]   ?? "").trim() !== "";
          const hasPhone   = !!phoneKey   && String(args[phoneKey]   ?? "").trim() !== "";
          const hasAddress = !!addressKey && String(args[addressKey] ?? "").trim() !== "";
          return (
            <div key={group ?? "_anon"} className="flex items-center justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium truncate">{label}</div>
                {currentName ? (
                  <div className="text-[10px] text-muted-foreground flex items-center gap-1.5 min-w-0">
                    <span className="truncate">{currentName}</span>
                    {(hasEmail || hasPhone || hasAddress) && (
                      <span className="inline-flex items-center gap-1 shrink-0 text-muted-foreground/70">
                        {hasEmail   && <Mail   className="w-3 h-3" aria-label="email filled"   />}
                        {hasPhone   && <Phone  className="w-3 h-3" aria-label="phone filled"   />}
                        {hasAddress && <MapPin className="w-3 h-3" aria-label="address filled" />}
                      </span>
                    )}
                  </div>
                ) : (
                  <div className="text-[10px] text-muted-foreground italic">
                    Pick a contact or type a name in the field below
                  </div>
                )}
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <RecipientPicker
                  allArgKeys={argKeys}
                  nameKey={nameKey}
                  onPick={(r) => fillGroup(groupKeys, r)}
                  // Per-group fill replaces the address-sibling logic;
                  // pass explicit null so the picker doesn't ALSO try
                  // the legacy sibling write.
                  precomputedAddressKey={null}
                  groupLabel={label}
                />
                <GroupExtractor
                  targets={extractTargets}
                  onExtracted={(values) => onArgsChange({ ...args, ...values })}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}


function GroupExtractor({
  targets, onExtracted,
}: {
  targets: Array<{ key: string; label: string; pattern?: string }>;
  onExtracted: (values: Record<string, string>) => void;
}) {
  const [open, setOpen] = useState<"paste" | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<"extract" | "upload" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [lastFilled, setLastFilled] = useState<string[] | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  async function runExtract() {
    if (!text.trim() || busy) return;
    setBusy("extract");
    setErr(null);
    try {
      const r = await api.post<{ values: Record<string, string> }>(
        "/api/compose/extract-fields",
        { text: text.trim(), fields: targets },
      );
      const filled = Object.keys(r.values || {});
      if (filled.length === 0) {
        setErr("Nothing found in the pasted text that matches this slot.");
      } else {
        onExtracted(r.values);
        setLastFilled(filled);
        setText("");
        setOpen(null);
      }
    } catch (e: any) {
      setErr(e?.message || "Extract failed.");
    } finally {
      setBusy(null);
    }
  }

  async function onFile(f: File) {
    if (busy) return;
    setBusy("upload");
    setErr(null);
    try {
      const fd = new FormData();
      fd.append("file", f);
      fd.append("fields_json", JSON.stringify(targets));
      const r = await fetch("/api/compose/extract-from-upload", {
        method: "POST", body: fd, credentials: "include",
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({} as any));
        throw new Error(j.detail || `HTTP ${r.status}`);
      }
      const data = await r.json() as { values: Record<string, string> };
      const filled = Object.keys(data.values || {});
      if (filled.length === 0) {
        setErr("Nothing extractable in that file for this slot.");
      } else {
        onExtracted(data.values);
        setLastFilled(filled);
      }
    } catch (e: any) {
      setErr(e?.message || "Upload failed.");
    } finally {
      setBusy(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <div className="inline-flex items-center gap-1">
      <button
        type="button"
        onClick={() => { setOpen(open === "paste" ? null : "paste"); setErr(null); }}
        disabled={!!busy}
        className={cn(
          "text-xs md:text-[9px] inline-flex items-center gap-1 px-2.5 md:px-1.5 h-8 md:h-auto md:py-0.5 rounded-full transition border",
          open === "paste"
            ? "bg-sky-500/20 text-sky-600 border-sky-500/40"
            : "bg-sky-500/10 text-sky-600 border-sky-500/20 hover:bg-sky-500/15",
        )}
        title="Paste contact details / a document text — LLM extracts only this slot's fields"
      >
        <Copy className="w-3.5 h-3.5 md:w-2.5 md:h-2.5" />
        <span className="md:hidden">Paste</span>
        <span className="hidden md:inline">paste</span>
      </button>
      <button
        type="button"
        onClick={() => fileRef.current?.click()}
        disabled={!!busy}
        className="text-xs md:text-[9px] inline-flex items-center gap-1 px-2.5 md:px-1.5 h-8 md:h-auto md:py-0.5 rounded-full transition border bg-emerald-500/10 text-emerald-600 border-emerald-500/20 hover:bg-emerald-500/15"
        title="Upload a document (PDF / Word / text) — LLM extracts only this slot's fields"
      >
        {busy === "upload"
          ? <Loader2 className="w-3.5 h-3.5 md:w-2.5 md:h-2.5 animate-spin" />
          : <Upload className="w-3.5 h-3.5 md:w-2.5 md:h-2.5" />}
        <span className="md:hidden">Upload</span>
        <span className="hidden md:inline">upload</span>
      </button>
      <input
        ref={fileRef}
        type="file"
        accept=".pdf,.docx,.txt,.md,text/*,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        className="hidden"
        onChange={e => {
          const f = e.target.files?.[0];
          if (f) void onFile(f);
        }}
      />
      {open === "paste" && (
        <div className="absolute z-50 mt-8 ml-0 left-0 w-[28rem] max-w-[90vw] bg-popover border border-border rounded-md shadow-lg p-3 space-y-2">
          <textarea
            value={text}
            onChange={e => setText(e.target.value)}
            placeholder={`Paste anything — contact card, copied email signature, snipped letter header. Extra info is fine; the LLM only takes the fields it can confidently match: ${targets.map(t => t.label).join(", ")}.`}
            className="w-full min-h-[120px] text-xs rounded-md border border-border bg-background px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-ring/30 resize-y"
            autoFocus
          />
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10px] text-muted-foreground">
              Targets: {targets.map(t => t.key).join(", ")}
            </span>
            <div className="flex gap-1.5">
              <button
                type="button"
                onClick={() => { setOpen(null); setText(""); setErr(null); }}
                className="px-2.5 py-1 text-xs rounded-md bg-muted hover:bg-muted/70"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={runExtract}
                disabled={!text.trim() || busy === "extract"}
                className="px-2.5 py-1 text-xs rounded-md font-medium bg-sky-500 hover:bg-sky-600 text-white disabled:opacity-50 inline-flex items-center gap-1"
              >
                {busy === "extract" ? <Loader2 className="w-3 h-3 animate-spin" /> : <Sparkles className="w-3 h-3" />}
                Extract
              </button>
            </div>
          </div>
        </div>
      )}
      {err && (
        <span className="text-[10px] text-rose-600 ml-1">{err}</span>
      )}
      {lastFilled && !err && (
        <span className="text-[10px] text-emerald-600 ml-1">filled: {lastFilled.join(", ")}</span>
      )}
    </div>
  );
}


// Splits the args list into THREE groups so the user scans top-to-bottom
// in order of priority:
//
//   1. Required & in the letter   → MUST fill; visible in the rendered body
//   2. Optional & in the letter   → nice to fill; visible if non-empty
//   3. Envelope & metadata        → subject, recipient email, customer
//                                   numbers — used for sending or PDF
//                                   header but NEVER in the body.
//                                   May still be required (e.g. an email
//                                   subject) but its absence from the
//                                   body is what defines this group.
//
// Each row also carries its own red asterisk when the schema says
// required — grouping picks the most urgent fields out at a glance,
// the asterisk reinforces the signal once you're scrolled past the
// top group.
//
// Detection is a static regex on body_html for the body/non-body split —
// anything matched here is classified "in the letter", intentionally
// conservative (even {% if args.X %} guards count). The required split
// uses fieldSchema (from template.ask_user_for_args) — legacy templates
// without that schema fall through with required=false everywhere, so
// every "in the letter" field ends up in the optional pile.
function ArgsList({
  argKeys, args, numbering, setArg,
  isWritableTextArg, isAutoSubjectArg, autoGenPending,
  setWriteArgFor, autoGenerateArg,
  bodyHtml, fieldSchema,
}: {
  argKeys: string[];
  args: Record<string, unknown>;
  numbering: Record<string, NumberingMatch>;
  setArg: (k: string, v: unknown) => void;
  isWritableTextArg: (k: string, v: unknown) => boolean;
  isAutoSubjectArg: (k: string) => boolean;
  autoGenPending: string | null;
  setWriteArgFor: (k: string) => void;
  autoGenerateArg: (k: string) => void;
  bodyHtml: string;
  fieldSchema: Record<string, {
    label?: string;
    required?: boolean;
    role?: string;
    hidden_when_positions_set?: boolean;
    item_schema?: Array<{
      key: string; label?: string; type?: string; required?: boolean;
      default?: unknown; hint?: string;
    }>;
    min_items?: number;
    hint?: string;
  }>;
}) {
  const isReferenced = useMemo(() => {
    const direct = new Set<string>();
    const prefixes: string[] = [];
    if (!bodyHtml) return (_: string) => false;
    // (a) Direct: {{ args.foo }} or bare args.foo inside Jinja control
    // flow ({% if args.foo %}, args.foo | filter, etc).
    const reDirect = /\bargs\.([A-Za-z_][A-Za-z0-9_]*)/g;
    let m: RegExpExecArray | null;
    while ((m = reDirect.exec(bodyHtml)) !== null) direct.add(m[1]);
    // (b) Bracket-string: args.get('foo'), args.get("foo").
    const reBracket = /args\.get\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]/g;
    while ((m = reBracket.exec(bodyHtml)) !== null) direct.add(m[1]);
    // (c) Dynamic concat: args.get('foo_' ~ i ~ '_bar') — the invoice
    // line-item pattern. Treat every arg whose key starts with the
    // captured prefix as referenced; without this, position_1_*,
    // position_2_*, etc. fall into the non-altering pile even though
    // they obviously render in the invoice.
    const rePrefix = /args\.get\(\s*['"]([A-Za-z_][A-Za-z0-9_]*_)['"]\s*~/g;
    while ((m = rePrefix.exec(bodyHtml)) !== null) prefixes.push(m[1]);
    return (key: string) =>
      direct.has(key) || prefixes.some(p => key.startsWith(p));
  }, [bodyHtml]);

  // Detect whether positions[] is non-empty in args. When it is, the
  // legacy position_<N>_<suffix> rows tagged with
  // hidden_when_positions_set are filtered out — they'd be a confusing
  // second control surface for the same data, and the renderer's
  // has_flat guard would silently prefer them over the new array.
  const positionsValue = args["positions"];
  const positionsInUse = Array.isArray(positionsValue) && positionsValue.length > 0;

  const requiredAltering: string[] = [];
  const optionalAltering: string[] = [];
  const envelope: string[] = [];
  for (const k of argKeys) {
    const s = fieldSchema[k];
    if (positionsInUse && s?.hidden_when_positions_set) continue;
    if (isReferenced(k)) {
      (s?.required ? requiredAltering : optionalAltering).push(k);
    } else {
      envelope.push(k);
    }
  }

  const inputFor = (k: string) => {
    const s = fieldSchema[k];
    if (s?.role === "line_items" && Array.isArray(s.item_schema)) {
      return (
        <LineItemsEditor
          key={k}
          k={k}
          v={args[k]}
          label={s.label || k}
          required={!!s.required}
          hint={s.hint}
          itemSchema={s.item_schema}
          minItems={s.min_items}
          onChange={(v) => setArg(k, v)}
        />
      );
    }
    return (
      <ArgInput
        key={k}
        k={k}
        v={args[k]}
        label={s?.label}
        required={!!s?.required}
        numbering={numbering[k]}
        onChange={(v) => setArg(k, v)}
        allArgKeys={argKeys}
        onWriteArg={isWritableTextArg(k, args[k]) ? () => setWriteArgFor(k) : undefined}
        onAutoGenerate={isAutoSubjectArg(k) ? () => autoGenerateArg(k) : undefined}
        autoGenerating={autoGenPending === k}
      />
    );
  };

  // Header strip helper — consistent visual weight across the three
  // tiers, hairline above for everything except the very first section.
  const sectionHeader = (
    title: string, hint: string, showHairline: boolean,
  ) => (
    <div className={showHairline ? "pt-4 mt-3 border-t border-border/60" : ""}>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground/70 mb-1">
        {title}
      </div>
      <div className="text-[10px] text-muted-foreground/60 mb-2">
        {hint}
      </div>
    </div>
  );

  return (
    <div className="space-y-2">
      {requiredAltering.length > 0 && (
        <>
          {sectionHeader(
            "Required",
            "Pflichtfelder — Yorik fills the document with these.",
            false,
          )}
          {requiredAltering.map(inputFor)}
        </>
      )}
      {optionalAltering.length > 0 && (
        <>
          {sectionHeader(
            "Optional (appears in the letter)",
            "Nice-to-haves — left empty, the template skips them.",
            requiredAltering.length > 0,
          )}
          {optionalAltering.map(inputFor)}
        </>
      )}
      {envelope.length > 0 && (
        <>
          {sectionHeader(
            "Envelope & metadata",
            "Subject, recipient email, customer numbers — for sending or PDF header, not the body.",
            requiredAltering.length + optionalAltering.length > 0,
          )}
          {envelope.map(inputFor)}
        </>
      )}
    </div>
  );
}


// Dynamic line-items editor — renders one row per element of args[k]
// (an array of objects), where each row's fields come from the
// template author's item_schema declaration. Add / remove rows
// freely; the renderer's positions[]-first branch picks up the new
// shape immediately. Sub-round 2 of Fix 8 — replaces the hardcoded
// position_1..5_* flat-key grid for invoice templates.
function LineItemsEditor({
  k, v, label, required, hint, itemSchema, minItems, onChange,
}: {
  k: string;
  v: unknown;
  label: string;
  required: boolean;
  hint?: string;
  itemSchema: Array<{
    key: string; label?: string; type?: string; required?: boolean;
    default?: unknown; hint?: string;
  }>;
  minItems?: number;
  onChange: (next: Array<Record<string, unknown>>) => void;
}) {
  const items: Array<Record<string, unknown>> = useMemo(() => {
    if (!Array.isArray(v)) return [];
    return v.map((row) =>
      row && typeof row === "object" && !Array.isArray(row)
        ? (row as Record<string, unknown>)
        : {}
    );
  }, [v]);

  const emptyRow = useCallback((): Record<string, unknown> => {
    const r: Record<string, unknown> = {};
    for (const f of itemSchema) {
      if (f.default !== undefined) r[f.key] = f.default;
    }
    return r;
  }, [itemSchema]);

  const updateRow = (rowIdx: number, fieldKey: string, fieldVal: unknown) => {
    const next = items.map((row, i) =>
      i === rowIdx ? { ...row, [fieldKey]: fieldVal } : row
    );
    onChange(next);
  };
  const addRow = () => onChange([...items, emptyRow()]);
  const removeRow = (rowIdx: number) => {
    const next = items.filter((_, i) => i !== rowIdx);
    onChange(next);
  };
  const moveRow = (rowIdx: number, dir: -1 | 1) => {
    const target = rowIdx + dir;
    if (target < 0 || target >= items.length) return;
    const next = [...items];
    [next[rowIdx], next[target]] = [next[target], next[rowIdx]];
    onChange(next);
  };

  const labelWithRequired = (
    <>
      {label}
      {required && (
        <span className="ml-0.5 text-rose-500 font-medium"
              aria-label="Pflichtfeld" title="Pflichtfeld">*</span>
      )}
    </>
  );

  const renderField = (
    rowIdx: number, row: Record<string, unknown>,
    field: typeof itemSchema[0],
  ) => {
    const val = row[field.key];
    const required = !!field.required;
    const isNum = field.type === "number";
    const isArea = field.type === "textarea";
    const commonCls =
      "w-full rounded border border-border bg-background px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-ring";
    const inputEl = isArea ? (
      <textarea
        value={typeof val === "string" ? val : ""}
        onChange={(e) => updateRow(rowIdx, field.key, e.target.value)}
        rows={2}
        className={commonCls}
        placeholder={field.hint || ""}
      />
    ) : (
      <input
        type={isNum ? "number" : "text"}
        value={
          val === undefined || val === null ? ""
            : isNum && typeof val === "number" ? String(val)
            : isNum && typeof val === "string" ? val
            : typeof val === "string" ? val : String(val)
        }
        onChange={(e) => {
          const raw = e.target.value;
          if (isNum) {
            if (raw === "") return updateRow(rowIdx, field.key, "");
            const n = Number(raw.replace(",", "."));
            updateRow(rowIdx, field.key, Number.isNaN(n) ? raw : n);
          } else {
            updateRow(rowIdx, field.key, raw);
          }
        }}
        className={commonCls}
        placeholder={field.hint || ""}
        inputMode={isNum ? "decimal" : "text"}
      />
    );
    return (
      <div key={field.key} className="flex-1 min-w-[120px]">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground/70 mb-0.5">
          {field.label || field.key}
          {required && (
            <span className="text-rose-500 ml-0.5">*</span>
          )}
        </div>
        {inputEl}
      </div>
    );
  };

  const canRemove = !minItems || items.length > minItems;

  return (
    <div className="py-2">
      <div className="flex items-baseline justify-between mb-1.5">
        <div className="text-xs font-medium">{labelWithRequired}</div>
        <button
          type="button"
          onClick={addRow}
          className="text-xs text-primary hover:underline"
        >
          + Add row
        </button>
      </div>
      {hint && (
        <div className="text-[10px] text-muted-foreground/70 mb-2">{hint}</div>
      )}
      <div className="space-y-3">
        {items.length === 0 && (
          <div className="rounded border border-dashed border-border/60 p-3 text-center">
            <div className="text-[11px] text-muted-foreground mb-2">
              No rows yet. Add one to start.
            </div>
            <button
              type="button"
              onClick={addRow}
              className="text-xs text-primary hover:underline"
            >+ Add the first row</button>
          </div>
        )}
        {items.map((row, rowIdx) => (
          <div
            key={rowIdx}
            className="rounded border border-border/70 bg-muted/20 p-2"
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-[10px] text-muted-foreground">
                Row {rowIdx + 1}
              </span>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => moveRow(rowIdx, -1)}
                  disabled={rowIdx === 0}
                  className="text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-30 px-1"
                  aria-label="Move up"
                  title="Move up"
                >↑</button>
                <button
                  type="button"
                  onClick={() => moveRow(rowIdx, 1)}
                  disabled={rowIdx === items.length - 1}
                  className="text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-30 px-1"
                  aria-label="Move down"
                  title="Move down"
                >↓</button>
                <button
                  type="button"
                  onClick={() => removeRow(rowIdx)}
                  disabled={!canRemove}
                  className="text-[11px] text-rose-500 hover:text-rose-600 disabled:opacity-30 px-1"
                  aria-label="Remove row"
                  title={canRemove ? "Remove this row" : `At least ${minItems} row(s) required`}
                >×</button>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {itemSchema.map((field) => renderField(rowIdx, row, field))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}


function ArgInput({ k, v, label, required, numbering, onChange, allArgKeys, onWriteArg, onAutoGenerate, autoGenerating }:
  {
    k: string;
    v: unknown;
    // Human-readable label from template.ask_user_for_args[k].label.
    // Falls back to the raw key when absent so legacy templates that
    // never declared labels keep rendering the same as before.
    label?: string;
    // Required marker (red asterisk after the label). The grouping
    // already puts required fields at the top, but the asterisk
    // reinforces the signal inside the row itself — easier to keep
    // track of when the user is scrolled past the section header.
    required?: boolean;
    numbering?: NumberingMatch;
    onChange: (v: unknown) => void;
    allArgKeys?: string[];
    // Optional "Write with AI" button shown next to long-content args
    // (body_text, notes, ...). When set, clicking opens a modal in the
    // parent that asks the LLM to write the field's content from
    // scratch — structural alternative to the selection-based revise
    // flow that kept misinterpreting template stubs as context.
    onWriteArg?: () => void;
    // One-click auto-gen for subject-shape fields (betreff/subject/title).
    // No modal — clicking fires the LLM call with body context. Mutually
    // exclusive with onWriteArg in practice (parent decides which to pass).
    onAutoGenerate?: () => void;
    autoGenerating?: boolean;
  }) {
  // The from-contacts picker + per-group paste extractor used to live
  // here as inline chips next to each recipient_name arg row. As of
  // 2026-06-02 they moved to the dedicated RecipientGroupsSection
  // rendered above the arg list, so picking a contact fills every
  // group-tagged slot (name + address + email + phone + iban + tax_id)
  // in one shot instead of forcing the user to repeat-pick per arg.
  const isNumber = typeof v === "number";
  const isBool = typeof v === "boolean";

  // Display label: prefer the template-declared label, fall back to the
  // raw key. The raw-key fallback intentionally keeps the original look
  // for templates that haven't declared ask_user_for_args.
  const displayLabel = label || k;
  const labelWithRequired = (
    <>
      {displayLabel}
      {required && (
        <span
          className="ml-0.5 text-rose-500 font-medium"
          aria-label="Pflichtfeld"
          title="Pflichtfeld"
        >*</span>
      )}
    </>
  );

  if (isBool) {
    return (
      <label className="flex items-center justify-between text-xs cursor-pointer">
        <span className="text-muted-foreground">{labelWithRequired}</span>
        <input
          type="checkbox"
          checked={!!v}
          onChange={e => onChange(e.target.checked)}
          className="accent-rose-500"
        />
      </label>
    );
  }

  return (
    <label className="block">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] text-muted-foreground">{labelWithRequired}</span>
        <div className="flex items-center gap-1">
          {numbering && (
            <span
              className="text-[9px] inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 border border-emerald-500/20"
              title={`Auto-allocated from the ${numbering.kind} series on Save / Send`}
            >
              <Hash className="w-2 h-2" />
              next in {numbering.kind}
            </span>
          )}
          {onWriteArg && (
            <button
              type="button"
              onClick={onWriteArg}
              className="text-xs md:text-[9px] inline-flex items-center gap-1 px-2.5 md:px-1.5 h-8 md:h-auto md:py-0.5 rounded-full transition border bg-violet-500/10 text-violet-600 border-violet-500/20 hover:bg-violet-500/20"
              title="Yorik will write this field for you — just tell him what it's about."
              aria-label="Write field with AI"
            >
              <Sparkles className="w-3.5 h-3.5 md:w-2.5 md:h-2.5" />
              <span className="md:hidden">Write with AI</span>
              <span className="hidden md:inline">AI</span>
            </button>
          )}
          {onAutoGenerate && (
            <button
              type="button"
              onClick={onAutoGenerate}
              disabled={autoGenerating}
              className="text-xs md:text-[9px] inline-flex items-center gap-1 px-2.5 md:px-1.5 h-8 md:h-auto md:py-0.5 rounded-full transition border bg-violet-500/10 text-violet-600 border-violet-500/20 hover:bg-violet-500/20 disabled:opacity-60 disabled:cursor-wait"
              title="Yorik suggests a subject — based on the current letter text. No typing needed, just click."
              aria-label="Auto-generate subject"
            >
              {autoGenerating
                ? <Loader2 className="w-3.5 h-3.5 md:w-2.5 md:h-2.5 animate-spin" />
                : <Sparkles className="w-3.5 h-3.5 md:w-2.5 md:h-2.5" />}
              <span className="md:hidden">{autoGenerating ? "Generating…" : "Suggest subject"}</span>
              <span className="hidden md:inline">{autoGenerating ? "…" : "Auto"}</span>
            </button>
          )}
        </div>
      </div>
      {(() => {
        const stringValue = String(v ?? "");
        // Multi-line if the value contains a newline OR is a long string
        // (addresses, paragraph args). Single-line input collapses \n —
        // looked like data corruption in the args panel when an address
        // had its line1/postcode/city joined.
        const multiline = !isNumber && (stringValue.includes("\n") || stringValue.length > 80);
        if (multiline) {
          const rows = Math.min(6, Math.max(2, stringValue.split("\n").length));
          return (
            <textarea
              value={stringValue}
              onChange={e => onChange(e.target.value)}
              rows={rows}
              className={cn(
                "mt-0.5 w-full px-2 py-1 rounded-md text-xs leading-tight resize-y focus:outline-none focus:ring-2 focus:ring-ring/30 transition whitespace-pre-wrap",
                numbering
                  ? "bg-emerald-500/5 border border-emerald-500/20 font-mono"
                  : "bg-muted/60 focus:bg-muted",
              )}
            />
          );
        }
        return (
          <input
            type={isNumber ? "number" : "text"}
            value={stringValue}
            onChange={e => onChange(isNumber ? Number(e.target.value) : e.target.value)}
            className={cn(
              "mt-0.5 w-full h-8 px-2 rounded-md text-xs focus:outline-none focus:ring-2 focus:ring-ring/30 transition",
              numbering
                ? "bg-emerald-500/5 border border-emerald-500/20 font-mono"
                : "bg-muted/60 focus:bg-muted",
            )}
          />
        );
      })()}
    </label>
  );
}

function DataRow({ k, v }: { k: string; v: unknown }) {
  let preview: string;
  if (v == null) preview = "—";
  else if (Array.isArray(v)) preview = `${v.length} item${v.length === 1 ? "" : "s"}`;
  else if (typeof v === "object") preview = "{ … }";
  else preview = String(v);
  if (preview.length > 60) preview = preview.slice(0, 57) + "…";
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <span className="text-muted-foreground truncate min-w-0">{k}</span>
      <span className="text-foreground/80 font-mono text-[11px] text-right">{preview}</span>
    </div>
  );
}

function EmptyAi({ onOpenNumbering, templateCount }: { onOpenNumbering: () => void; templateCount: number }) {
  // Branch on whether the user actually has any templates to pick from.
  // Before this branch, the right pane said "Pick a template to see its
  // arguments…" while the left sidebar said "No templates yet" — two
  // contradictory CTAs that left a fresh user stuck.
  if (templateCount === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center text-center text-xs text-muted-foreground p-8">
        <Sparkles className="w-8 h-8 mb-3 opacity-30" />
        <div className="text-sm font-medium text-foreground mb-1">No templates yet</div>
        <div className="text-[11px] max-w-xs leading-relaxed">
          Use <strong className="text-foreground/80">Browse community templates</strong> in the
          sidebar to pull a ready-made template, or drop a JSON template into the
          <code className="text-foreground/80">templates/</code> folder.
        </div>
        <button
          onClick={onOpenNumbering}
          className="mt-6 inline-flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-muted/40 hover:bg-muted text-muted-foreground hover:text-foreground transition"
        >
          <Hash className="w-3 h-3" /> Document numbering
        </button>
      </div>
    );
  }
  return (
    <div className="flex-1 flex flex-col items-center justify-center text-center text-xs text-muted-foreground p-8">
      <Sparkles className="w-8 h-8 mb-3 opacity-30" />
      Pick a template to see its arguments and the data it pulled.
      <button
        onClick={onOpenNumbering}
        className="mt-6 inline-flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-md bg-muted/40 hover:bg-muted text-muted-foreground hover:text-foreground transition"
      >
        <Hash className="w-3 h-3" /> Document numbering
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Save / Send dialogs
// ---------------------------------------------------------------------------

function SaveDialog({
  defaultTitle, defaultTags, busy, onSave, onClose,
}: {
  defaultTitle: string;
  defaultTags: string[];
  busy: boolean;
  onSave: (p: { title: string; tags: string[] }) => void;
  onClose: () => void;
}) {
  const [title, setTitle] = useState(defaultTitle);
  const [tagsInput, setTagsInput] = useState(defaultTags.join(", "));

  useEffect(() => {
    function esc(e: KeyboardEvent) { if (e.key === "Escape" && !busy) onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose, busy]);

  return (
    <DialogShell title="Save to Paperless" icon={Save} onClose={onClose} busy={busy}>
      <Field label="Title">
        <input
          autoFocus
          value={title}
          onChange={e => setTitle(e.target.value)}
          className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
        />
      </Field>
      <Field label="Tags (comma-separated)">
        <input
          value={tagsInput}
          onChange={e => setTagsInput(e.target.value)}
          className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
        />
      </Field>
      <DialogFooter
        primary="Save"
        busy={busy}
        disabled={!title.trim()}
        onPrimary={() => onSave({
          title: title.trim(),
          tags: tagsInput.split(",").map(s => s.trim()).filter(Boolean),
        })}
        onCancel={onClose}
        primaryColor="rose"
      />
    </DialogShell>
  );
}

function SendDialog({
  defaultTo, defaultSubject, defaultTitle, defaultTags, defaultDelivery,
  accounts, defaultAccountId,
  busy, onSend, onClose,
}: {
  /** Recipient email prefilled from the template's recipient_email arg
   *  (lookup is by role, so it works for any key name). Empty when no
   *  such arg exists or the user hasn't filled it. */
  defaultTo: string;
  defaultSubject: string;
  defaultTitle: string;
  defaultTags: string[];
  /** Pre-selected delivery mode from the active template's delivery_default.
   *  Falls back to "attachment" for formal-letter safety. */
  defaultDelivery: "attachment" | "inline";
  /** All configured email accounts. When length > 1, the dialog shows a
   *  "From" select so the user can pick which mailbox sends. Length ≤ 1
   *  hides the select and falls back to defaultAccountId (or undefined =
   *  legacy single-credential connector path). */
  accounts: Array<{ id: number; email: string; display_name?: string | null; is_default?: boolean }>;
  defaultAccountId: number | undefined;
  busy: boolean;
  onSend: (p: {
    to: string; subject: string; body_text: string; title: string;
    tags: string[]; also_save: boolean;
    delivery: "attachment" | "inline";
    account_id?: number;
  }) => void;
  onClose: () => void;
}) {
  const [to, setTo] = useState(defaultTo);
  const [subject, setSubject] = useState(defaultSubject);
  const [body, setBody] = useState("");
  const [tagsInput, setTagsInput] = useState(defaultTags.join(", "));
  const [alsoSave, setAlsoSave] = useState(true);
  const [delivery, setDelivery] = useState<"attachment" | "inline">(defaultDelivery);
  const [accountId, setAccountId] = useState<number | undefined>(defaultAccountId);

  useEffect(() => {
    function esc(e: KeyboardEvent) { if (e.key === "Escape" && !busy) onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose, busy]);

  const validEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(to.trim());
  const isInline = delivery === "inline";

  return (
    <DialogShell title="Send via email" icon={Send} onClose={onClose} busy={busy}>
      {accounts.length > 1 && (
        <Field label="From">
          <select
            value={accountId ?? ""}
            onChange={e => setAccountId(e.target.value ? Number(e.target.value) : undefined)}
            className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
          >
            {accounts.map(a => (
              <option key={a.id} value={a.id}>
                {a.display_name ? `${a.display_name} <${a.email}>` : a.email}
                {a.is_default ? " · default" : ""}
              </option>
            ))}
          </select>
        </Field>
      )}
      <Field label="To">
        <input
          autoFocus={!defaultTo}
          type="email"
          value={to}
          onChange={e => setTo(e.target.value)}
          placeholder="recipient@example.com"
          className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
        />
      </Field>
      <Field label="Subject">
        <input
          value={subject}
          onChange={e => setSubject(e.target.value)}
          className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
        />
      </Field>
      <Field label="Delivery">
        <div className="grid grid-cols-2 gap-2">
          <DeliveryOption
            active={delivery === "attachment"}
            icon="📎"
            title="PDF attachment"
            sub="DIN-5008 layout, formal"
            onClick={() => setDelivery("attachment")}
          />
          <DeliveryOption
            active={delivery === "inline"}
            icon="📧"
            title="Inline email body"
            sub="No attachment, looks like a normal mail"
            onClick={() => setDelivery("inline")}
          />
        </div>
      </Field>
      <Field label={isInline
                     ? "Plain-text fallback (for old mail clients)"
                     : "Body (optional — PDF is attached)"}>
        <textarea
          value={body}
          onChange={e => setBody(e.target.value)}
          rows={3}
          placeholder={isInline
            ? "Leave empty — Yorik will strip tags from the rendered letter for you."
            : `(See attached: ${defaultTitle}.pdf)`}
          className="w-full px-3 py-2 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition resize-none"
        />
      </Field>
      <Field label="Tags for Paperless copy">
        <input
          value={tagsInput}
          onChange={e => setTagsInput(e.target.value)}
          className="w-full h-9 px-3 rounded-md bg-muted/60 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
        />
      </Field>
      <label className="flex items-center gap-2 text-xs text-muted-foreground cursor-pointer">
        <input
          type="checkbox"
          checked={alsoSave}
          onChange={e => setAlsoSave(e.target.checked)}
          className="accent-rose-500"
        />
        Also save a copy to Paperless
      </label>
      <DialogFooter
        primary="Send"
        busy={busy}
        disabled={!validEmail || !subject.trim()}
        onPrimary={() => onSend({
          to: to.trim(),
          subject: subject.trim(),
          body_text: body,
          title: defaultTitle,
          tags: tagsInput.split(",").map(s => s.trim()).filter(Boolean),
          also_save: alsoSave,
          delivery,
          account_id: accountId,
        })}
        onCancel={onClose}
        primaryColor="rose"
      />
    </DialogShell>
  );
}

function DeliveryOption({
  active, icon, title, sub, onClick,
}: {
  active: boolean; icon: string; title: string; sub: string; onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "text-left px-3 py-2 rounded-md border text-xs transition",
        active
          ? "bg-rose-500/10 border-rose-500/40 text-foreground"
          : "bg-muted/30 border-border text-muted-foreground hover:bg-muted/60",
      )}
    >
      <div className="flex items-center gap-1.5 font-medium text-[13px]">
        <span>{icon}</span> {title}
      </div>
      <div className="text-[11px] mt-0.5 opacity-80">{sub}</div>
    </button>
  );
}

function SidebarTabButton({
  active, onClick, label, count,
}: { active: boolean; onClick: () => void; label: string; count: number }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "relative px-3 py-2 text-xs font-medium transition flex items-center gap-1.5",
        active
          ? "text-foreground"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      {label}
      <span className={cn(
        "text-[10px] px-1.5 rounded-full",
        active ? "bg-rose-500/15 text-rose-600" : "bg-muted text-muted-foreground",
      )}>{count}</span>
      {active && (
        <span className="absolute -bottom-px left-0 right-0 h-0.5 bg-rose-500 rounded-t" />
      )}
    </button>
  );
}

// Sender-arg prefill — mutates `args` in place. The patterns match
// the keys the in-tree templates actually use (absender_name,
// sender_address, absender_email, …) plus a few common variants we
// want to support if a community template ships with English keys.
//
// Empty-only fill: never overwrite a key the template author
// already set to a non-empty default (e.g. a fixed business address).
function prefillSenderArgs(
  args: Record<string, unknown>,
  user: Record<string, unknown> | undefined,
): void {
  if (!user) return;
  const name = [user.first_name, user.last_name].filter(Boolean).join(" ")
    || (user.name as string) || "";
  const addressParts = [
    user.address_street as string,
    [user.address_postcode, user.address_city].filter(Boolean).join(" "),
  ].filter(Boolean);
  const address = addressParts.join("\n");
  const phone   = (user.phone as string) || "";
  const email   = (user.email as string) || "";
  const company = (user.business_name as string) || "";

  const mapping: Record<string, string> = {
    // German variants
    absender_name:     name,
    absender_adresse:  address,
    absender_strasse:  (user.address_street as string) || "",
    absender_plz:      (user.address_postcode as string) || "",
    absender_ort:      (user.address_city as string) || "",
    absender_email:    email,
    absender_telefon:  phone,
    absender_firma:    company,
    // English variants for community templates
    sender_name:       name,
    sender_address:    address,
    sender_email:      email,
    sender_phone:      phone,
    from_name:         name,
    from_address:      address,
  };

  for (const [key, value] of Object.entries(mapping)) {
    if (!(key in args)) continue;
    const current = args[key];
    if (current != null && String(current).trim() !== "") continue;
    if (value) args[key] = value;
  }
}


// Compute readiness from a template's `ask_user_for_args` schema.
// Returns the required-field count, how many are filled, and the
// first label that's still empty (for the footer hint).
function computeReadiness(
  template: ComposeTemplate | null,
  args: Record<string, unknown>,
): { required: number; filled: number; firstMissingLabel: string | null } {
  if (!template?.ask_user_for_args?.length) {
    return { required: 0, filled: 0, firstMissingLabel: null };
  }
  const required = template.ask_user_for_args.filter(f => f.required);
  let filled = 0;
  let firstMissing: string | null = null;
  for (const f of required) {
    const v = args[f.key];
    const ok = v != null && String(v).trim() !== "";
    if (ok) {
      filled += 1;
    } else if (firstMissing === null) {
      firstMissing = f.label || f.key;
    }
  }
  return { required: required.length, filled, firstMissingLabel: firstMissing };
}


// ── Footer ──────────────────────────────────────────────────────────
// One primary CTA (Send), one "More actions" chevron menu for the
// secondary paths (Save to Paperless · Export PDF), and a readiness
// chip on the left so the user knows whether the draft is actually
// complete before they hit Send.
function ComposeFooter({
  activeTemplate, args, editorHasContent,
  pdfBusy, saveBusy, sendBusy,
  onExportPdf, onOpenSave, onOpenSend,
}: {
  activeTemplate: ComposeTemplate | null;
  args: Record<string, unknown>;
  editorHasContent: boolean;
  pdfBusy: boolean;
  saveBusy: boolean;
  sendBusy: boolean;
  onExportPdf: () => void;
  onOpenSave: () => void;
  onOpenSend: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Click-away to close the chevron menu.
  useEffect(() => {
    if (!menuOpen) return;
    function onDoc(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [menuOpen]);

  const readiness = computeReadiness(activeTemplate, args);
  const allRequiredFilled = readiness.required === 0
    || readiness.filled === readiness.required;

  return (
    <footer
      // Safe-area padding so the footer (and the Send button) clears
      // the iPhone home-indicator gesture zone.
      className="border-t border-border px-3 md:px-6 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] bg-background/80 backdrop-blur flex items-center justify-between gap-2 md:gap-3 shrink-0"
    >
      <div className="text-xs truncate flex items-center gap-2">
        {activeTemplate ? (
          readiness.required === 0 ? (
            <>
              <span className="text-muted-foreground">Template</span>
              <span className="text-foreground font-medium">{activeTemplate.name}</span>
            </>
          ) : allRequiredFilled ? (
            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border border-emerald-500/20 text-[11px] font-medium">
              <CheckCircle2 className="w-3 h-3" />
              Ready · {readiness.required}/{readiness.required} required fields filled
            </span>
          ) : (
            <span
              className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-700 dark:text-amber-400 border border-amber-500/20 text-[11px] font-medium min-w-0"
              title={`First missing field: ${readiness.firstMissingLabel}`}
            >
              <AlertCircle className="w-3 h-3 shrink-0" />
              {/* Count hidden on mobile — at 375px viewport the
                  full "3/5 required fields · Recipient name missing"
                  truncated awkwardly. Mobile shows just the most
                  useful piece (the missing field). */}
              <span className="hidden sm:inline">
                {readiness.filled}/{readiness.required} required fields
              </span>
              {readiness.firstMissingLabel && (
                <span className="text-amber-600/80 dark:text-amber-400/80 font-normal truncate">
                  <span className="hidden sm:inline">· </span>{readiness.firstMissingLabel} missing
                </span>
              )}
            </span>
          )
        ) : (
          <span className="text-muted-foreground">
            {editorHasContent
              ? "Blank document — typing freely"
              : "Pick a template on the left, or just start typing"}
          </span>
        )}
      </div>

      <div className="flex items-center gap-2 shrink-0">
        {/* Secondary chevron menu — Save / Export. Hidden behind a
            single button so the primary Send action stands alone. */}
        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setMenuOpen(o => !o)}
            disabled={!editorHasContent}
            className={cn(
              "inline-flex items-center gap-1.5 text-xs h-11 md:h-8 px-3 md:px-2.5 rounded-md transition",
              editorHasContent
                ? "bg-muted hover:bg-muted/70 text-foreground"
                : "bg-muted/40 text-muted-foreground/50 cursor-not-allowed",
            )}
            title="More actions"
            aria-label="More actions (save, export)"
          >
            More
            <ChevronUp className={cn(
              "w-3 h-3 transition-transform",
              menuOpen ? "rotate-0" : "rotate-180",
            )} />
          </button>
          {menuOpen && (
            <div className="absolute right-0 bottom-full mb-1 min-w-[200px] rounded-md border border-border bg-popover shadow-lg z-30 overflow-hidden">
              <button
                onClick={() => { setMenuOpen(false); onOpenSave(); }}
                disabled={saveBusy}
                className="w-full px-3 py-2 text-left text-xs flex items-center gap-2 hover:bg-muted disabled:opacity-50"
              >
                {saveBusy
                  ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  : <Save className="w-3.5 h-3.5" />}
                Save to Paperless
              </button>
              <button
                onClick={() => { setMenuOpen(false); onExportPdf(); }}
                disabled={pdfBusy}
                className="w-full px-3 py-2 text-left text-xs flex items-center gap-2 hover:bg-muted disabled:opacity-50 border-t border-border"
              >
                {pdfBusy
                  ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  : <Download className="w-3.5 h-3.5" />}
                Export as PDF
              </button>
            </div>
          )}
        </div>

        {/* Primary CTA. Stays enabled even when required fields are
            missing — the chip warns; the user decides. */}
        <button
          onClick={onOpenSend}
          disabled={!editorHasContent || sendBusy}
          className={cn(
            "inline-flex items-center gap-1.5 text-xs md:text-xs h-11 md:h-8 px-5 md:px-4 rounded-md font-semibold transition",
            editorHasContent && !sendBusy
              ? "bg-rose-500 hover:bg-rose-600 text-white shadow-sm"
              : "bg-muted/40 text-muted-foreground/50 cursor-not-allowed",
          )}
          aria-label="Send draft"
        >
          {sendBusy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
          Send
        </button>
      </div>
    </footer>
  );
}


// Right-pane tab — Arguments | Ask Yorik. Slim variant (no count chip)
// because there's nothing meaningful to count for these two surfaces.
// Carries an `unread` dot when a chat reply lands while the user is
// looking at the Arguments tab.
function RightPaneTab({
  active, onClick, label, unread,
}: { active: boolean; onClick: () => void; label: string; unread?: boolean }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "relative flex-1 px-3 py-2 text-xs font-medium transition flex items-center justify-center gap-1.5 rounded-md",
        active
          ? "text-foreground bg-muted/60"
          : "text-muted-foreground hover:text-foreground hover:bg-muted/40",
      )}
    >
      {label}
      {unread && !active && (
        <span className="w-1.5 h-1.5 rounded-full bg-rose-500" />
      )}
    </button>
  );
}

interface DraftSidebarItem {
  id: number;
  user_id: number;
  kind: string;
  template_id: string | null;
  recipient: string | null;
  subject: string | null;
  created_at: string;
  updated_at: string;
}

function DraftsListPanel({
  drafts, loading, activeDraftId, onPick, onDelete,
}: {
  drafts: DraftSidebarItem[];
  loading: boolean;
  activeDraftId: number | null;
  onPick: (id: number) => void;
  onDelete: (id: number) => void;
}) {
  if (loading && drafts.length === 0) {
    return (
      <div className="px-2 space-y-3 pt-2">
        {[1, 2, 3].map(i => (
          <div key={i} className="p-3 animate-pulse space-y-2">
            <div className="h-3 bg-muted/60 rounded w-2/3" />
            <div className="h-2.5 bg-muted/40 rounded w-full" />
          </div>
        ))}
      </div>
    );
  }
  if (drafts.length === 0) {
    return (
      <div className="px-4 py-8 text-center">
        <div className="w-12 h-12 mx-auto mb-3 rounded-2xl bg-gradient-to-br from-rose-500/20 to-violet-500/20 flex items-center justify-center">
          <FileText className="w-5 h-5 text-rose-500" />
        </div>
        <div className="text-sm font-medium text-foreground mb-1">No drafts</div>
        <div className="text-[11px] text-muted-foreground leading-relaxed">
          Every letter you start with Yorik lands here automatically — you can
          reopen and finish them any time.
        </div>
      </div>
    );
  }
  return (
    <>
      {drafts.map(d => {
        const isActive = activeDraftId === d.id;
        const subtitle = d.subject?.trim() || d.template_id || "(no subject)";
        return (
          <div
            key={d.id}
            className={cn(
              "w-full text-left px-3 py-2.5 rounded-lg transition group relative",
              isActive ? "bg-sidebar-accent shadow-sm" : "hover:bg-sidebar-accent/50",
            )}
          >
            <button
              onClick={() => onPick(d.id)}
              className="w-full text-left"
            >
              <div className="flex items-center gap-2">
                <div className={cn(
                  "w-7 h-7 rounded-md flex items-center justify-center shrink-0",
                  isActive
                    ? "bg-rose-500/20 text-rose-500"
                    : "bg-muted/60 text-muted-foreground group-hover:text-foreground",
                )}>
                  <FileText className="w-3.5 h-3.5" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium truncate">
                    {d.recipient?.trim() || "(unknown recipient)"}
                  </div>
                  <div className="text-[10px] text-muted-foreground mt-0.5 truncate">
                    {subtitle}
                  </div>
                </div>
              </div>
              <div className="text-[10px] text-muted-foreground mt-1.5 flex items-center gap-1.5">
                <span>{relativeTime(d.updated_at)}</span>
                <span className="opacity-50">·</span>
                <span className="uppercase tracking-wider">{d.kind}</span>
              </div>
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onDelete(d.id); }}
              className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition p-1 rounded hover:bg-red-500/10 text-muted-foreground hover:text-red-600"
              title="Delete draft"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        );
      })}
    </>
  );
}

function relativeTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso.replace(" ", "T") + (iso.endsWith("Z") ? "" : "Z"));
  const diffMs = Date.now() - d.getTime();
  const diffM = Math.floor(diffMs / 60000);
  if (diffM < 1) return "just now";
  if (diffM < 60) return `${diffM} min ago`;
  const diffH = Math.floor(diffM / 60);
  if (diffH < 24) return `${diffH} h ago`;
  const diffD = Math.floor(diffH / 24);
  if (diffD < 7) return `${diffD} day${diffD === 1 ? "" : "s"} ago`;
  return d.toLocaleDateString("en-US", { day: "2-digit", month: "short", year: "2-digit" });
}

/** One-time, dismissible banner under the editor — shown ONLY when the
 *  user hasn't uploaded a signature image yet. Sits below editor_notes,
 *  above ComposeAgentChat. Dismissal sticks via localStorage so users
 *  who knowingly skip aren't nagged on every compose visit.
 *
 *  We don't ask in the chat (that'd nag) and we don't block sending
 *  (the fallback "_________________" line renders fine). This is a soft
 *  upgrade hint — once uploaded, the signature appears in every letter. */
const _SIG_DISMISS_KEY = "yorik.compose.signatureHintDismissed";

function SignatureUpsellBanner() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try { return localStorage.getItem(_SIG_DISMISS_KEY) === "1"; }
    catch { return false; }
  });

  if (user?.signature_data_url) return null;  // signature already uploaded
  if (dismissed) return null;

  function dismiss() {
    setDismissed(true);
    try { localStorage.setItem(_SIG_DISMISS_KEY, "1"); } catch {}
  }

  return (
    <div className="border-t border-border bg-violet-500/5 dark:bg-violet-500/10 shrink-0">
      <div className="max-w-[820px] mx-auto px-6 py-2.5 flex items-center gap-3">
        <span className="text-base shrink-0">✍️</span>
        <div className="flex-1 min-w-0 text-xs text-foreground/85">
          <span className="font-medium">Upload your signature once</span>
          <span className="text-muted-foreground"> — it then appears automatically in every letter instead of the "_______" line.</span>
        </div>
        <button
          onClick={() => navigate("/settings")}
          className="text-[11px] px-2.5 py-1 rounded-md bg-violet-500/15 hover:bg-violet-500/25 text-violet-600 dark:text-violet-300 font-medium transition shrink-0"
        >
          Open in Settings
        </button>
        <button
          onClick={dismiss}
          className="text-muted-foreground hover:text-foreground transition p-0.5 shrink-0"
          title="Don't show again"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  );
}

function ConfirmReplaceTemplateModal({
  incomingName, currentName, onConfirm, onCancel,
}: {
  incomingName: string;
  currentName: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel();
      if (e.key === "Enter") onConfirm();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onConfirm, onCancel]);

  return (
    <DialogShell
      title="Replace template?"
      icon={AlertCircle}
      busy={false}
      onClose={onCancel}
    >
      <p className="text-sm text-foreground/85 leading-relaxed">
        You've already edited the current letter
        {currentName ? <> (<span className="font-medium">{currentName}</span>)</> : null}.
        Switching to <span className="font-medium">"{incomingName}"</span> will
        discard your changes.
      </p>
      <p className="text-xs text-muted-foreground">
        Tip: save the current letter first with <kbd className="px-1 py-0.5 rounded bg-muted text-[10px]">Save</kbd>, then switch templates — you'll find it anytime under <span className="font-medium">Drafts</span>.
      </p>
      <DialogFooter
        primary="Replace"
        busy={false}
        disabled={false}
        onPrimary={onConfirm}
        onCancel={onCancel}
        primaryColor="rose"
      />
    </DialogShell>
  );
}

function DialogShell({
  title, icon: Icon, busy, children, onClose,
}: {
  title: string;
  icon: React.ComponentType<{ className?: string }>;
  busy: boolean;
  children: React.ReactNode;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-[1000] bg-black/60 backdrop-blur-sm flex items-center justify-center p-6"
      onClick={() => { if (!busy) onClose(); }}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-md overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Icon className="w-4 h-4 text-rose-500" />
            <span className="font-semibold">{title}</span>
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
          {children}
        </div>
      </div>
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

function DialogFooter({
  primary, busy, disabled, onPrimary, onCancel, primaryColor,
}: {
  primary: string;
  busy: boolean;
  disabled: boolean;
  onPrimary: () => void;
  onCancel: () => void;
  primaryColor: "rose";
}) {
  const ringClass = primaryColor === "rose"
    ? "bg-rose-500 hover:bg-rose-600 text-white"
    : "bg-primary text-primary-foreground";
  return (
    <div className="flex items-center justify-end gap-2 pt-2">
      <button
        onClick={onCancel}
        disabled={busy}
        className="px-3 py-1.5 text-xs rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-50"
      >
        Cancel
      </button>
      <button
        onClick={onPrimary}
        disabled={busy || disabled}
        className={cn(
          "px-3 py-1.5 text-xs rounded-md font-medium transition inline-flex items-center gap-1.5",
          !busy && !disabled ? ringClass : "bg-muted text-muted-foreground cursor-not-allowed",
        )}
      >
        {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : primary}
      </button>
    </div>
  );
}


// ───────────────────────── community templates dialog ───────────────────────

interface CommunityTemplate {
  id: string;
  name: string;
  description: string;
  tags: string[];
  vertical?: string | null;
  author: string;
  version: string;
  needs_apps?: string[];
  // ISO 3166-1 alpha-2 codes the template is valid for, or ["*"] for
  // universal. Empty/missing is also treated as universal so old
  // manifests don't get hidden when filtering.
  countries?: string[];
  locale?: string | null;
  category?: string | null;
}

interface CatalogueResponse {
  templates: CommunityTemplate[];
  source: string;
  fetched_at: string | null;
  cached: boolean;
  error: string | null;
}

function CommunityTemplatesDialog({
  installedIds, onInstalled, onClose, toast,
}: {
  installedIds: Set<string>;
  onInstalled: () => void;
  onClose: () => void;
  toast: (msg: string, kind?: ToastKind) => void;
}) {
  const [data, setData] = useState<CatalogueResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [installing, setInstalling] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  // null = "All countries". Otherwise an ISO 3166-1 alpha-2 code. The
  // chip row below is auto-derived from the catalogue's countries union,
  // plus an explicit "Universal" chip for ["*"] / empty.
  const [countryFilter, setCountryFilter] = useState<string | null>(null);

  const fetchCatalogue = useCallback(async (force = false) => {
    setLoading(true);
    try {
      const r = await api.get<CatalogueResponse>(
        `/api/compose/community/templates${force ? "?refresh=true" : ""}`,
      );
      setData(r);
    } catch (e: any) {
      toast(`Couldn't load community catalogue: ${e?.message || e}`, "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { fetchCatalogue(); }, [fetchCatalogue]);

  // Available country chips: union of every template's countries,
  // minus the universal marker. Sorted alphabetically.
  const availableCountries = useMemo(() => {
    const set = new Set<string>();
    for (const t of data?.templates || []) {
      for (const c of t.countries || []) {
        if (c !== "*") set.add(c);
      }
    }
    return Array.from(set).sort();
  }, [data]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = data?.templates || [];
    if (countryFilter) {
      // Show templates valid for this country, plus universal ones —
      // a Mietminderung user still benefits from seeing the friend-letter.
      list = list.filter(t => {
        const cs = t.countries || [];
        if (cs.length === 0 || cs.includes("*")) return true;  // universal
        return cs.includes(countryFilter);
      });
    }
    if (q) {
      list = list.filter(t =>
        t.name.toLowerCase().includes(q)
        || t.description.toLowerCase().includes(q)
        || t.tags.some(tag => tag.toLowerCase().includes(q))
      );
    }
    return list;
  }, [data, query, countryFilter]);

  const install = useCallback(async (t: CommunityTemplate) => {
    setInstalling(t.id);
    try {
      await api.post(`/api/compose/community/install`, { id: t.id });
      toast(`Installed "${t.name}"`, "success");
      onInstalled();
    } catch (e: any) {
      toast(`Install failed: ${e?.message || e}`, "error");
    } finally {
      setInstalling(null);
    }
  }, [toast, onInstalled]);

  return (
    <div className="fixed inset-0 z-[400] bg-black/50 backdrop-blur-sm flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="w-full max-w-2xl max-h-[80vh] bg-card border border-border rounded-2xl shadow-2xl flex flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center gap-3 shrink-0">
          <div className="w-9 h-9 rounded-full bg-rose-500/15 flex items-center justify-center">
            <Globe className="w-4 h-4 text-rose-500" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="font-semibold leading-none">Community templates</div>
            <div className="text-[11px] text-muted-foreground mt-1 truncate">
              {data?.source || "loading…"}
            </div>
          </div>
          <button
            onClick={() => fetchCatalogue(true)}
            disabled={loading}
            title="Force-refresh from GitHub (bypass 5min cache)"
            className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted disabled:opacity-50"
          >
            <RefreshCw className={cn("w-4 h-4", loading && "animate-spin")} />
          </button>
          <button onClick={onClose} className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted">
            <X className="w-4 h-4" />
          </button>
        </header>

        {data?.error && (
          <div className="px-5 py-3 bg-amber-500/[0.06] border-b border-amber-500/30 text-xs flex items-start gap-2">
            <AlertCircle className="w-3.5 h-3.5 text-amber-500 mt-0.5 shrink-0" />
            <div className="text-foreground/80 min-w-0">
              <div className="font-medium text-amber-600 mb-0.5">
                Couldn't reach the community catalogue
              </div>
              <div className="text-muted-foreground break-all">{data.error}</div>
              <div className="text-muted-foreground mt-1">
                Set <code className="text-foreground">YORIK_COMMUNITY_TEMPLATES_URL</code> to point at a different catalogue
                (a private repo's raw URL, or a <code>file://</code> path for local testing).
              </div>
            </div>
          </div>
        )}

        {availableCountries.length > 0 && (
          <div className="px-5 py-2 border-b border-border shrink-0 flex flex-wrap gap-1.5 items-center">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mr-1">
              Country
            </span>
            <button
              onClick={() => setCountryFilter(null)}
              className={cn(
                "text-[11px] px-2.5 py-1 rounded-full border transition",
                countryFilter === null
                  ? "bg-rose-500 text-white border-rose-500"
                  : "bg-card border-border text-muted-foreground hover:text-foreground",
              )}
            >
              All
            </button>
            {availableCountries.map(cc => (
              <button
                key={cc}
                onClick={() => setCountryFilter(cc)}
                title={`Templates valid in ${cc} (plus universal ones)`}
                className={cn(
                  "text-[11px] px-2.5 py-1 rounded-full border transition font-mono uppercase",
                  countryFilter === cc
                    ? "bg-rose-500 text-white border-rose-500"
                    : "bg-card border-border text-muted-foreground hover:text-foreground",
                )}
              >
                {cc}
              </button>
            ))}
          </div>
        )}

        <div className="px-5 py-3 border-b border-border shrink-0">
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search by name, description, or tag…"
            className="w-full h-9 px-3 rounded-md bg-muted/40 border border-transparent focus:border-border focus:bg-background text-sm focus:outline-none"
          />
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading && !data && (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin mr-2" /> Loading catalogue…
            </div>
          )}
          {!loading && filtered.length === 0 && !data?.error && (
            <div className="text-center py-12 text-muted-foreground text-sm italic">
              {query ? "No templates match your search." : "Catalogue is empty."}
            </div>
          )}
          <div className="divide-y divide-border/50">
            {filtered.map(t => {
              const isInstalled = installedIds.has(t.id);
              const isBusy = installing === t.id;
              return (
                <div key={t.id} className="px-5 py-3 flex items-start gap-3 hover:bg-muted/20">
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-sm flex items-center gap-2">
                      <span className="truncate">{t.name}</span>
                      {isInstalled && (
                        <span className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-500 text-[10px] font-medium">
                          <CheckCircle2 className="w-2.5 h-2.5" /> installed
                        </span>
                      )}
                    </div>
                    {t.description && (
                      <div className="text-xs text-muted-foreground mt-0.5 line-clamp-2">{t.description}</div>
                    )}
                    <div className="text-[10px] text-muted-foreground mt-1 flex items-center gap-2 flex-wrap">
                      <span>by {t.author}</span>
                      <span>·</span>
                      <span>v{t.version}</span>
                      {(() => {
                        const cs = t.countries || [];
                        const isUniversal = cs.length === 0 || cs.includes("*");
                        return (
                          <>
                            <span>·</span>
                            {isUniversal ? (
                              <span
                                className="px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-500 font-medium"
                                title="Universal — works in any country"
                              >
                                🌍 universal
                              </span>
                            ) : (
                              cs.map(c => (
                                <span
                                  key={c}
                                  className="px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-600 font-mono font-medium"
                                  title={`Valid in ${c}`}
                                >
                                  {c}
                                </span>
                              ))
                            )}
                          </>
                        );
                      })()}
                      {t.tags.length > 0 && (
                        <>
                          <span>·</span>
                          {t.tags.slice(0, 4).map(tag => (
                            <span key={tag} className="px-1.5 py-0.5 rounded bg-muted text-foreground/70">
                              #{tag}
                            </span>
                          ))}
                        </>
                      )}
                    </div>
                  </div>
                  <button
                    onClick={() => install(t)}
                    disabled={isInstalled || isBusy}
                    className={cn(
                      "shrink-0 text-xs px-3 py-1.5 rounded-md font-medium transition inline-flex items-center gap-1.5",
                      isInstalled
                        ? "bg-muted text-muted-foreground cursor-default"
                        : "bg-rose-500 hover:bg-rose-600 text-white shadow-sm",
                      isBusy && "opacity-60",
                    )}
                  >
                    {isBusy
                      ? <Loader2 className="w-3 h-3 animate-spin" />
                      : isInstalled
                        ? <Check className="w-3 h-3" />
                        : <Download className="w-3 h-3" />}
                    {isInstalled ? "Installed" : isBusy ? "Installing…" : "Install"}
                  </button>
                </div>
              );
            })}
          </div>
        </div>

        <footer className="px-5 py-3 border-t border-border bg-muted/20 text-[11px] text-muted-foreground flex items-center justify-between shrink-0">
          <div>
            {data && !data.error && (
              <>{data.templates.length} template{data.templates.length === 1 ? "" : "s"} available
                · cache {data.cached ? "hit" : "miss"}
                {data.fetched_at && ` · fetched ${data.fetched_at}`}</>
            )}
          </div>
          {data?.source && !data.source.startsWith("file://") && (
            <a
              href={data.source.replace(/\/[^/]+$/, "")}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 hover:text-foreground"
            >
              View on GitHub <ExternalLink className="w-3 h-3" />
            </a>
          )}
        </footer>
      </div>
    </div>
  );
}
