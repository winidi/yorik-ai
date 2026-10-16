/**
 * SharingCard — Settings → You. "What may Beate see of mine?" per area
 * (tasks, calendar, contacts, documents), read or edit, and what the
 * others share with me. Admins can set it for restricted accounts (kids).
 * Backend: backend/sharing_routes.py.
 */
import { useCallback, useEffect, useState } from "react";
import { Loader2, Users } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

type Share = { areas: string[]; level: "read" | "write" | null };
type Member = { user_id: string; name: string; role: string; i_share: Share; shares_with_me: Share };
type Status = { owner_id: string; areas: string[]; members: Member[]; can_set_family_default?: boolean };

const LABEL: Record<string, string> = { tasks: "settings.sharing.areas.tasks", calendar: "settings.sharing.areas.calendar", contacts: "settings.sharing.areas.contacts", documents: "settings.sharing.areas.documents" };

export function SharingCard({ toast, ownerId, title }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
  ownerId?: string;      // admin editing a restricted account's sharing
  title?: string;
}) {
  const { t } = useTranslation();
  const areaLabel = (a: string) => (LABEL[a] ? t(LABEL[a]) : a);
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // Operator only: who is left out of the family-calendar default.
  const [leftOut, setLeftOut] = useState<Set<string>>(new Set());
  const q = ownerId ? `?owner=${encodeURIComponent(ownerId)}` : "";

  const load = useCallback(async () => {
    try {
      setStatus(await api.get<Status>(`/api/sharing${q}`));
    } catch (e: any) {
      toast(t("settings.sharing.loadFailed", { msg: e.message }), "error");
    }
  }, [q, toast, t]);
  useEffect(() => { load(); }, [load]);

  async function save(m: Member, areas: string[], level: "read" | "write") {
    setBusy(m.user_id);
    try {
      await api.put(`/api/sharing/${m.user_id}${q}`, { areas, level });
      await load();
    } catch (e: any) {
      toast(t("settings.sharing.saveFailed", { msg: e?.message || e }), "error");
    } finally {
      setBusy(null);
    }
  }

  async function shareFamilyCalendars() {
    setBusy("family");
    try {
      const r = await api.post<{ added: number }>("/api/sharing/family-calendars", { exclude_user_ids: [...leftOut] });
      toast(r.added ? t("settings.sharing.familyShared", { n: r.added }) : t("settings.sharing.alreadyShared"), "success");
      await load();
    } catch (e: any) {
      toast(t("settings.sharing.shareFailed", { msg: e?.message || e }), "error");
    } finally {
      setBusy(null);
    }
  }

  if (!status) return null;

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <h3 className="text-xs font-semibold text-muted-foreground mb-3">{title || t("settings.sharing.title")}</h3>
      <div className="mb-3 flex items-start gap-2">
        <Users className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">{ownerId ? t("settings.sharing.whoSeesAccount") : t("settings.sharing.whoSeesYours")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("settings.sharing.intro")}
          </p>
        </div>
      </div>

      {status.members.length === 0 && <p className="text-xs text-muted-foreground">{t("settings.sharing.noMembers")}</p>}

      {!ownerId && status.can_set_family_default && status.members.length > 0 && (
        <div className="mb-4 rounded-lg border border-border bg-muted/30 p-3">
          <div className="text-sm font-medium">{t("settings.sharing.familyCalendars")}</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            {t("settings.sharing.familyCalendarsIntro")}
          </p>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
            {status.members.map(m => (
              <label key={m.user_id} className="flex items-center gap-1.5 text-xs">
                <input
                  type="checkbox"
                  checked={leftOut.has(m.user_id)}
                  onChange={e => setLeftOut(prev => {
                    const n = new Set(prev);
                    if (e.target.checked) n.add(m.user_id); else n.delete(m.user_id);
                    return n;
                  })}
                  className="w-3.5 h-3.5 accent-violet-500"
                />
                {t("settings.sharing.leaveOut", { name: m.name })}
              </label>
            ))}
          </div>
          <button
            onClick={shareFamilyCalendars}
            disabled={busy === "family"}
            className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-60"
          >
            {busy === "family" && <Loader2 className="w-3 h-3 animate-spin" />}
            {t("settings.sharing.shareFamily")}
          </button>
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-muted-foreground">
              <th className="text-left font-normal py-1 pr-2">{t("settings.sharing.member")}</th>
              {status.areas.map(a => <th key={a} className="font-normal py-1 px-1">{areaLabel(a)}</th>)}
              <th className="font-normal py-1 px-1">{t("settings.sharing.canEdit")}</th>
              <th className="text-left font-normal py-1 pl-3">{ownerId ? t("settings.sharing.theyShareWithThem") : t("settings.sharing.theyShareWithMe")}</th>
            </tr>
          </thead>
          <tbody>
            {status.members.map(m => {
              const areas = m.i_share.areas;
              const level = m.i_share.level || "read";
              const isBusy = busy === m.user_id;
              return (
                <tr key={m.user_id} className={cn("border-t border-border", isBusy && "opacity-60")}>
                  <td className="py-2 pr-2">
                    <div className="font-medium truncate max-w-[10rem]">{m.name}</div>
                    <div className="text-2xs text-muted-foreground">{m.role}</div>
                  </td>
                  {status.areas.map(a => (
                    <td key={a} className="text-center px-1">
                      <input
                        type="checkbox"
                        checked={areas.includes(a)}
                        disabled={isBusy}
                        onChange={e => save(m, e.target.checked ? [...areas, a] : areas.filter(x => x !== a), level)}
                        className="w-4 h-4 accent-violet-500"
                      />
                    </td>
                  ))}
                  <td className="text-center px-1">
                    <input
                      type="checkbox"
                      checked={level === "write" && areas.length > 0}
                      disabled={isBusy || areas.length === 0}
                      onChange={e => save(m, areas, e.target.checked ? "write" : "read")}
                      className="w-4 h-4 accent-violet-500"
                    />
                  </td>
                  <td className="pl-3 text-xs text-muted-foreground">
                    {isBusy ? <Loader2 className="w-3 h-3 animate-spin" /> :
                      m.shares_with_me.areas.length
                        ? `${m.shares_with_me.areas.map(areaLabel).join(", ")}${m.shares_with_me.level === "write" ? ` ${t("settings.sharing.editSuffix")}` : ""}`
                        : t("settings.sharing.nothing")}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
