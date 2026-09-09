/**
 * SharingCard — Settings → You. "What may Beate see of mine?" per area
 * (tasks, calendar, contacts, documents), read or edit, and what the
 * others share with me. Admins can set it for restricted accounts (kids).
 * Backend: backend/sharing_routes.py.
 */
import { useCallback, useEffect, useState } from "react";
import { Loader2, Users } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

type Share = { areas: string[]; level: "read" | "write" | null };
type Member = { user_id: string; name: string; role: string; i_share: Share; shares_with_me: Share };
type Status = { owner_id: string; areas: string[]; members: Member[] };

const LABEL: Record<string, string> = { tasks: "Tasks", calendar: "Calendar", contacts: "Contacts", documents: "Documents" };

export function SharingCard({ toast, ownerId, title }: {
  toast: (text: string, kind?: "info" | "success" | "error") => void;
  ownerId?: string;      // admin editing a restricted account's sharing
  title?: string;
}) {
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const q = ownerId ? `?owner=${encodeURIComponent(ownerId)}` : "";

  const load = useCallback(async () => {
    try {
      setStatus(await api.get<Status>(`/api/sharing${q}`));
    } catch (e: any) {
      toast(`Couldn't load sharing: ${e.message}`, "error");
    }
  }, [q, toast]);
  useEffect(() => { load(); }, [load]);

  async function save(m: Member, areas: string[], level: "read" | "write") {
    setBusy(m.user_id);
    try {
      await api.put(`/api/sharing/${m.user_id}${q}`, { areas, level });
      await load();
    } catch (e: any) {
      toast(`Couldn't save: ${e?.message || e}`, "error");
    } finally {
      setBusy(null);
    }
  }

  if (!status) return null;

  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <h3 className="text-xs uppercase tracking-wider font-semibold text-muted-foreground mb-3">{title || "Sharing"}</h3>
      <div className="mb-3 flex items-start gap-2">
        <Users className="w-4 h-4 text-violet-500 mt-0.5 shrink-0" />
        <div className="flex-1">
          <div className="text-sm font-medium">Who may see what of {ownerId ? "this account" : "yours"}</div>
          <p className="text-xs text-muted-foreground mt-0.5">
            Nothing is shared until you tick it, admins included. Tick an area to let a member see it;
            "can edit" also lets them change it. Documents and letters you mark as household are visible anyway.
          </p>
        </div>
      </div>

      {status.members.length === 0 && <p className="text-[11px] text-muted-foreground">No other members yet.</p>}

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[11px] text-muted-foreground">
              <th className="text-left font-normal py-1 pr-2">Member</th>
              {status.areas.map(a => <th key={a} className="font-normal py-1 px-1">{LABEL[a] || a}</th>)}
              <th className="font-normal py-1 px-1">Can edit</th>
              <th className="text-left font-normal py-1 pl-3">They share with {ownerId ? "them" : "me"}</th>
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
                    <div className="text-[10px] text-muted-foreground">{m.role}</div>
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
                  <td className="pl-3 text-[11px] text-muted-foreground">
                    {isBusy ? <Loader2 className="w-3 h-3 animate-spin" /> :
                      m.shares_with_me.areas.length
                        ? `${m.shares_with_me.areas.map(a => LABEL[a] || a).join(", ")}${m.shares_with_me.level === "write" ? " (edit)" : ""}`
                        : "nothing"}
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
