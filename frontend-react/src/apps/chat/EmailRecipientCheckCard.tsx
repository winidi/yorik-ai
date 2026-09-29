/**
 * EmailRecipientCheckCard — prepare_email held a mail back because the
 * address is unknown (never used, not in the contacts, not typed by the
 * person). The card shows the closest known addresses to pick, or lets
 * the person correct the address, then opens the Composer with it.
 *
 * Chat test 2026-09-27: "an seine web.de-Adresse" became an address
 * nobody ever used; Dirk wanted a choice, not a block.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Mail, ArrowRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { toast } from "@/components/Toast";

export function EmailRecipientCheckCard({ typed, suggestions, subject }: {
  typed: string; suggestions: string[]; subject?: string;
}) {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [value, setValue] = useState(typed);
  const [busy, setBusy] = useState(false);
  const options = [typed, ...suggestions.filter(s => s !== typed)];

  async function go() {
    const to = value.trim();
    if (!to || busy) return;
    setBusy(true);
    try {
      await api.post("/api/email/pending-draft/confirm", { to });
      navigate("/email?draft=pending");
    } catch (e: any) {
      toast(t("chat.email.failed", { error: e?.message || e }), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-2 max-w-xl rounded-xl border border-border bg-card p-4">
      <div className="flex items-start gap-3">
        <span className="grid place-items-center w-9 h-9 rounded-lg bg-primary/15 text-primary shrink-0">
          <Mail className="w-5 h-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="font-semibold">{t("chat.email.checkAddress")}</div>
          <div className="text-xs text-muted-foreground">
            {subject ? t("chat.email.neverWrittenSubject", { subject }) : t("chat.email.neverWritten")}
          </div>
        </div>
      </div>
      <div className="mt-3 space-y-1.5">
        {options.map(o => (
          <label key={o} className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="radio" name={`rcpt-${typed}`} checked={value === o} onChange={() => setValue(o)} />
            <span className="truncate">{o}</span>
            {o === typed && <span className="text-xs text-muted-foreground">{t("chat.email.asTyped")}</span>}
          </label>
        ))}
      </div>
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="mt-3 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
        aria-label={t("chat.email.recipientAddress")}
      />
      <div className="mt-3 flex justify-end">
        <button onClick={go} disabled={busy || !value.trim()}
                className="flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-medium disabled:opacity-50">
          {t("chat.email.useAndOpen")} <ArrowRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
