/**
 * EmailDraftReadyCard — what the chat shows when prepare_email staged
 * a new outgoing email: recipient, subject, a short preview, the
 * attachment name, and a button that jumps straight to the Email app.
 *
 * The draft itself already lives server-side (app_settings, read by
 * GET /api/email/pending-draft on the Composer's mount) by the time
 * this card renders — the button just navigates, it carries no data
 * of its own. Mirrors WritingDraftCard's letter/invoice pattern: a
 * real inline card with a direct way in, not a sentence telling the
 * user to go open another app themselves.
 */
import { useNavigate } from "react-router-dom";
import { Mail, ArrowRight } from "lucide-react";

export function EmailDraftReadyCard({ to, subject, preview, attachmentFilename }: {
  to: string; subject: string; preview?: string; attachmentFilename?: string;
}) {
  const navigate = useNavigate();
  return (
    <div className="mt-2 max-w-xl rounded-xl border border-border bg-card p-4">
      <div className="flex items-start gap-3">
        <span className="grid place-items-center w-9 h-9 rounded-lg bg-primary/15 text-primary shrink-0">
          <Mail className="w-5 h-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="font-semibold truncate">{subject || "(kein Betreff)"}</div>
          <div className="text-xs text-muted-foreground truncate">an {to}</div>
          {preview && <p className="mt-2 text-sm text-muted-foreground line-clamp-3">{preview}</p>}
          {attachmentFilename && (
            <div className="mt-2 text-xs text-muted-foreground truncate">📎 {attachmentFilename}</div>
          )}
        </div>
      </div>
      <div className="mt-3 flex justify-end">
        <button
          onClick={() => navigate("/email")}
          className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium"
        >
          Öffnen und senden <ArrowRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
