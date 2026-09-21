/**
 * WritingDraftCard — what the chat shows when Yorik wrote a letter
 * (`writing_draft_created` from the skill write_letter): who, what, the
 * first lines, what the sheet still marks as missing, and the way into
 * the Schreiben app. Nothing is asked here; missing things are filled in
 * on the sheet.
 */
import { useNavigate } from "react-router-dom";
import { FileText, PenLine } from "lucide-react";

export function WritingDraftCard({ documentId, recipient, subject, preview, missing }: {
  documentId: number; recipient: string; subject: string; preview: string; missing: string[];
}) {
  const navigate = useNavigate();
  return (
    <div className="mt-2 max-w-xl rounded-xl border border-border bg-card p-4">
      <div className="flex items-start gap-3">
        <span className="grid place-items-center w-9 h-9 rounded-lg bg-primary/15 text-primary shrink-0"><FileText className="w-5 h-5" /></span>
        <div className="min-w-0 flex-1">
          <div className="font-semibold truncate">{subject || "Brief"}</div>
          <div className="text-xs text-muted-foreground truncate">an {recipient || "— Empfänger fehlt"}</div>
          {preview && <p className="mt-2 text-sm text-muted-foreground line-clamp-3">{preview}</p>}
          {missing.length > 0 && <p className="mt-2 text-[11px] rounded-md bg-amber-500/15 text-amber-300 px-2 py-1 inline-block">Auf dem Blatt noch markiert: {missing.join(", ")}</p>}
        </div>
      </div>
      <div className="mt-3 flex justify-end">
        <button onClick={() => navigate(`/write?id=${documentId}`)} className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium">
          <PenLine className="w-4 h-4" /> Öffnen und bearbeiten
        </button>
      </div>
    </div>
  );
}
