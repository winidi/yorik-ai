/**
 * PdfFormEditModal — edit a PDF's AcroForm fields inline, before
 * sending. Renders each page with pdf.js (canvas + its own
 * AnnotationLayer for the real interactive form widgets — the same
 * building block Firefox's built-in PDF viewer uses), lets the user
 * type/click directly into the fields, and on "Speichern" replays the
 * edited values into the ORIGINAL bytes with pdf-lib to produce a new
 * PDF. The caller (Composer) swaps the attachment's content_b64 for
 * the result — no download, no re-attach, no leaving the compose
 * window.
 *
 * Field values live in pdf.js's own AnnotationStorage while the user
 * types (that wiring is automatic once a widget is rendered with
 * renderForms: true) keyed by each annotation's internal id, not its
 * human field name — so the id -> field-name map is built once from
 * page.getAnnotations() and used to translate back when saving.
 */
import { useEffect, useRef, useState } from "react";
import { X, Loader2, Save, ChevronLeft, ChevronRight } from "lucide-react";
import * as pdfjsLib from "pdfjs-dist";
// @ts-ignore -- no bundled types for the ?url import form
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { PDFLinkService } from "pdfjs-dist/web/pdf_viewer.mjs";
import "pdfjs-dist/web/pdf_viewer.css";
import { PDFDocument, PDFTextField, PDFCheckBox, PDFRadioGroup, PDFDropdown, PDFOptionList } from "pdf-lib";

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

const SCALE = 1.5;

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function bytesToB64(bytes: Uint8Array): string {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

export function PdfFormEditModal({ filename, contentB64, onSave, onClose }: {
  filename: string;
  contentB64: string;
  onSave: (newContentB64: string, newSize: number) => void;
  onClose: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [numPages, setNumPages] = useState(0);
  const [page, setPage] = useState(1);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const annotationDivRef = useRef<HTMLDivElement>(null);
  const originalBytesRef = useRef<Uint8Array | null>(null);
  const pdfDocRef = useRef<any>(null);
  const linkServiceRef = useRef<any>(null);
  // annotation id -> the field's fully-qualified PDF name, accumulated
  // across every page as it's rendered (a field can only live on one
  // page, but the user may visit pages in any order or not at all).
  const idToFieldNameRef = useRef<Map<string, string>>(new Map());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const bytes = b64ToBytes(contentB64);
        originalBytesRef.current = bytes;
        // pdf.js detaches/transfers typed arrays it's given in some
        // code paths — pass a fresh copy so originalBytesRef stays
        // intact for the pdf-lib save step later.
        const doc = await pdfjsLib.getDocument({ data: bytes.slice() }).promise;
        if (cancelled) return;
        pdfDocRef.current = doc;
        setNumPages(doc.numPages);
        const linkService = new PDFLinkService();
        linkService.setDocument(doc);
        linkServiceRef.current = linkService;
        setLoading(false);
      } catch (exc) {
        if (!cancelled) {
          setError(exc instanceof Error ? exc.message : "Konnte PDF nicht laden.");
          setLoading(false);
        }
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contentB64]);

  useEffect(() => {
    if (loading || error || !pdfDocRef.current) return;
    let cancelled = false;
    (async () => {
      const doc = pdfDocRef.current;
      const pdfPage = await doc.getPage(page);
      if (cancelled) return;
      const viewport = pdfPage.getViewport({ scale: SCALE });

      const canvas = canvasRef.current!;
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      const ctx = canvas.getContext("2d")!;
      // Tried disabling canvas annotation drawing to stop a filled text
      // field showing its baked-in value AND the live input on top —
      // but checkboxes/radios turn out to rely on the CANVAS appearance
      // for their checked glyph (the interactive input is only a
      // click target, invisible by itself in pdf_viewer.css), so
      // "Neuer Eintrag" silently lost its visible selected state.
      // Correctness there matters more than the text-field cosmetic
      // double-render, so this stays at the default (ENABLE). Revisit
      // if pdf.js exposes a way to disable it per-annotation-type.
      await pdfPage.render({ canvasContext: ctx, viewport }).promise;
      if (cancelled) return;

      const annotations = await pdfPage.getAnnotations({ intent: "display" });
      for (const a of annotations) {
        // pdf.js internal annotation data — fieldName is the field's
        // fully-qualified name (matches pdf-lib's form.getField(name)).
        if (a?.id && a?.fieldName) idToFieldNameRef.current.set(a.id, a.fieldName);
      }

      const div = annotationDivRef.current!;
      div.innerHTML = "";
      div.style.width = `${viewport.width}px`;
      div.style.height = `${viewport.height}px`;

      const layer = new pdfjsLib.AnnotationLayer({
        div, page: pdfPage, viewport,
        // Required by the constructor's destructure even when unused.
        accessibilityManager: null, annotationCanvasMap: null,
        annotationEditorUIManager: null, structTreeLayer: null,
        commentManager: null, linkService: linkServiceRef.current,
        annotationStorage: doc.annotationStorage,
      });
      await layer.render({
        viewport: viewport.clone({ dontFlip: true }),
        div, annotations, page: pdfPage,
        linkService: linkServiceRef.current,
        annotationStorage: doc.annotationStorage,
        renderForms: true,
        imageResourcesPath: "",
      });
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, error, page]);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const original = originalBytesRef.current!;
      const idMap = idToFieldNameRef.current;
      // AnnotationStorage has no getAll() — it's iterable directly
      // (Symbol.iterator over [id, value] pairs), like a Map.
      const storage = pdfDocRef.current.annotationStorage;

      const out = await PDFDocument.load(original);
      const form = out.getForm();

      if (storage) {
        for (const [id, entry] of storage) {
          const fieldName = idMap.get(id);
          if (!fieldName) continue;
          const value = entry?.value;
          if (value === undefined) continue;
          let field;
          try { field = form.getField(fieldName); } catch { continue; }
          try {
            if (field instanceof PDFTextField) {
              field.setText(value === null ? "" : String(value));
            } else if (field instanceof PDFCheckBox) {
              if (value) field.check(); else field.uncheck();
            } else if (field instanceof PDFRadioGroup) {
              if (typeof value === "string" && value) field.select(value);
            } else if (field instanceof PDFDropdown || field instanceof PDFOptionList) {
              const opts = Array.isArray(value) ? value : [String(value)];
              field.select(opts as string[]);
            }
          } catch (fieldExc) {
            // One field's value not matching pdf-lib's expectations
            // (e.g. an export string that changed shape) shouldn't
            // block saving everything else the user actually edited.
            console.warn(`PdfFormEditModal: could not apply field ${fieldName}`, fieldExc);
          }
        }
      }

      const newBytes = await out.save();
      onSave(bytesToB64(newBytes), newBytes.length);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Speichern fehlgeschlagen.");
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" onClick={onClose}>
      {/* pdf_viewer.css gives every widget input a translucent blue
          background (deliberately see-through, to mark "this is a
          fillable field") — fine for checkboxes/radios, whose checked
          glyph is drawn on the canvas UNDERNEATH and needs to show
          through. But the SAME canvas also draws each text field's
          current value as static text, so a translucent text input
          sitting on top of its own value doubles it visually. Opaque
          white behind text/select inputs only masks that duplicate
          without touching how checkboxes/radios render. */}
      <style>{`
        .pdf-form-edit-modal .textWidgetAnnotation :is(input, textarea),
        .pdf-form-edit-modal .choiceWidgetAnnotation select {
          /* pdf.js sets background-color: transparent as an INLINE
             style on the element itself, which beats any stylesheet
             rule regardless of specificity — !important is the only
             way to actually override it here. */
          background-color: #fff !important;
          background-image: none !important;
        }
      `}</style>
      <div
        className="pdf-form-edit-modal bg-background rounded-xl border border-border shadow-xl w-full max-w-3xl max-h-[90vh] flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="font-medium truncate">{filename}</div>
          <button onClick={onClose} className="p-1 rounded hover:bg-muted text-muted-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-auto flex items-center justify-center bg-muted/30 p-4">
          {loading && <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />}
          {error && <div className="text-sm text-rose-500 max-w-sm text-center">{error}</div>}
          {!loading && !error && (
            <div className="relative shadow-md" style={{ width: "fit-content" }}>
              <canvas ref={canvasRef} className="block" />
              <div ref={annotationDivRef} className="annotationLayer absolute inset-0" />
            </div>
          )}
        </div>

        <div className="flex items-center justify-between px-4 py-3 border-t border-border">
          <div className="flex items-center gap-2">
            <button
              disabled={page <= 1}
              onClick={() => setPage(p => Math.max(1, p - 1))}
              className="p-1.5 rounded-md border border-border disabled:opacity-40"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <span className="text-xs text-muted-foreground">Seite {page} / {numPages || "…"}</span>
            <button
              disabled={page >= numPages}
              onClick={() => setPage(p => Math.min(numPages, p + 1))}
              className="p-1.5 rounded-md border border-border disabled:opacity-40"
            >
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>
          <button
            onClick={handleSave}
            disabled={loading || saving || !!error}
            className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium disabled:opacity-50"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            Speichern
          </button>
        </div>
      </div>
    </div>
  );
}
