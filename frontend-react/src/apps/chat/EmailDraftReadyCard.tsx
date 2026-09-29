/**
 * EmailDraftReadyCard — what the chat shows when prepare_email staged
 * a new outgoing email: recipient, subject, a short preview, the
 * attachment name, and a button that jumps straight to the Email app.
 *
 * The draft itself already lives server-side (app_settings, read by
 * GET /api/email/pending-draft) by the time this card renders — the
 * button just navigates to /email?draft=pending, it carries no data of
 * its own. Only that link opens the draft; opening Email any other way
 * never does.
 *
 * With several own accounts and none chosen as default, the card asks
 * which one sends it before the button works (Dirk 2026-09-29: the
 * Composer used to take the first account without a word). Once the
 * mail is sent or thrown away the card says so, also after a reload.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Mail, ArrowRight, Check, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { toast } from "@/components/Toast";

interface Account { id: number; email: string; name?: string }

export function EmailDraftReadyCard({ to, subject, preview, attachmentFilename, accounts = [], accountId = null, done }: {
  to: string; subject: string; preview?: string; attachmentFilename?: string;
  /** The person's own accounts — only sent when there is a choice. */
  accounts?: Account[];
  accountId?: number | null;
  done?: string;
}) {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [chosen, setChosen] = useState<number | null>(accountId);
  const [makeDefault, setMakeDefault] = useState(false);
  const [saving, setSaving] = useState(false);
  const mustChoose = accounts.length > 1;

  async function choose(id: number, asDefault: boolean) {
    setSaving(true);
    try {
      await api.post("/api/email/pending-draft/account", { account_id: id, make_default: asDefault });
      setChosen(id);
    } catch (e: any) {
      toast(t("chat.email.failed", { error: e?.message || String(e) }), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mt-2 max-w-xl rounded-xl border border-border bg-card p-4">
      <div className="flex items-start gap-3">
        <span className="grid place-items-center w-9 h-9 rounded-lg bg-primary/15 text-primary shrink-0">
          <Mail className="w-5 h-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="font-semibold truncate">{subject || t("chat.email.noSubject")}</div>
          <div className="text-xs text-muted-foreground truncate">{t("chat.email.to", { to })}</div>
          {preview && <p className="mt-2 text-sm text-muted-foreground line-clamp-3">{preview}</p>}
          {attachmentFilename && (
            <div className="mt-2 text-xs text-muted-foreground truncate">📎 {attachmentFilename}</div>
          )}
        </div>
      </div>
      {done === "sent" ? (
        <div className="mt-3 flex justify-end">
          <span className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-primary">
            <Check className="w-4 h-4" /> {t("chat.email.sent")}
          </span>
        </div>
      ) : done === "discarded" ? (
        <div className="mt-3 flex justify-end">
          <span className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-muted-foreground">
            <X className="w-4 h-4" /> {t("chat.email.discarded")}
          </span>
        </div>
      ) : (
        <>
          {mustChoose && (
            <div className="mt-3 rounded-lg border border-border bg-background/60 p-2.5 text-sm">
              <label className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground shrink-0">{t("chat.email.from")}</span>
                <select
                  value={chosen ?? ""}
                  disabled={saving}
                  onChange={(e) => { const id = Number(e.target.value); if (id) void choose(id, makeDefault); }}
                  className="flex-1 min-w-0 rounded-md border border-border bg-background px-2 py-1 text-sm"
                >
                  <option value="" disabled>{t("chat.email.chooseAccount")}</option>
                  {accounts.map(a => (
                    <option key={a.id} value={a.id}>{a.name ? `${a.name} (${a.email})` : a.email}</option>
                  ))}
                </select>
              </label>
              <label className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={makeDefault}
                  disabled={saving}
                  onChange={(e) => {
                    setMakeDefault(e.target.checked);
                    if (e.target.checked && chosen) void choose(chosen, true);
                  }}
                />
                {t("chat.email.makeDefault")}
              </label>
            </div>
          )}
          <div className="mt-3 flex items-center justify-end gap-3">
            {mustChoose && !chosen && (
              <span className="text-xs text-muted-foreground">{t("chat.email.pickAccountFirst")}</span>
            )}
            <button
              onClick={() => navigate("/email?draft=pending")}
              disabled={mustChoose && !chosen}
              className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium disabled:opacity-50"
            >
              {t("chat.email.openAndSend")} <ArrowRight className="w-4 h-4" />
            </button>
          </div>
        </>
      )}
    </div>
  );
}
