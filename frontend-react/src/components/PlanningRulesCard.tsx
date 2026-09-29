/**
 * PlanningRulesCard — Settings → You. A few sentences the day planner
 * reads every time: who does what at home, working hours, how many
 * items a day. Chat corrections ("das macht Beate") land here too when
 * the user says "merk dir das". Backend: /api/profile/planning-rules.
 */
import { useCallback, useEffect, useState } from "react";
import { CalendarCheck, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { useTranslation } from "react-i18next";

export function PlanningRulesCard({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  const [rules, setRules] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api.get<{ rules: string }>("/api/profile/planning-rules");
      setRules(r.rules);
      setDraft(r.rules);
    } catch (e: any) {
      toast(t("planning.rules.loadFailed", { error: e.message }), "error");
    }
  }, [toast, t]);
  useEffect(() => { load(); }, [load]);

  async function save() {
    setBusy(true);
    try {
      const r = await api.patch<{ rules: string }>("/api/profile/planning-rules", { rules: draft });
      setRules(r.rules);
      setDraft(r.rules);
      toast(t("planning.rules.saved"), "success");
    } catch (e: any) {
      toast(t("planning.rules.saveFailed", { error: e?.message || e }), "error");
    } finally {
      setBusy(false);
    }
  }

  if (rules === null) return null;
  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <h3 className="text-xs font-semibold text-muted-foreground mb-3">{t("planning.rules.heading")}</h3>
      <div className="mb-3 flex items-start gap-2">
        <CalendarCheck className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">{t("planning.rules.title")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("planning.rules.intro")}
          </p>
        </div>
      </div>
      <textarea value={draft} onChange={e => setDraft(e.target.value)} rows={4}
                placeholder={t("planning.rules.placeholder")}
                className="w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm leading-relaxed" />
      <div className="mt-3 flex items-center gap-2">
        <button onClick={save} disabled={busy || draft === rules}
                className="flex items-center gap-2 rounded-lg bg-primary text-primary-foreground px-3 py-1.5 text-sm font-semibold disabled:opacity-50">
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : null} {t("common.save")}
        </button>
        {draft !== rules && <button onClick={() => setDraft(rules)} className="rounded-lg px-3 py-1.5 text-sm hover:bg-muted">{t("planning.rules.undo")}</button>}
      </div>
    </div>
  );
}
