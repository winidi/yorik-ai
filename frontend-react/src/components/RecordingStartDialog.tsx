/**
 * "Who is at the table?" — the one dialog before a recording starts.
 * Used by the kiosk tile (AmbientApp) and the Recordings page. Creates
 * the row and hands it to RecorderDock, which does the capturing.
 */
import { useEffect, useState } from "react";
import { Loader2, Mic, X } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/components/AuthGate";
import { createAndStart } from "@/components/RecorderDock";
import { cn } from "@/lib/utils";

type Member = { id: string; name: string };
const KINDS: Array<{ id: string; label: string }> = [
  { id: "dinner", label: "Dinner" },
  { id: "meeting", label: "Meeting" },
  { id: "conversation", label: "Conversation" },
];

export function RecordingStartDialog({ onClose, onStarted, defaultKind = "dinner", dark = false }: {
  onClose: () => void;
  onStarted?: (id: number) => void;
  defaultKind?: string;
  dark?: boolean;
}) {
  const auth = useAuth() as any;
  const me: string | undefined = auth?.user?.id;
  const [members, setMembers] = useState<Member[] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [kind, setKind] = useState(defaultKind);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<Member[]>("/api/users/assignable").then(list => {
      const others = list.filter(m => m.id !== me);
      setMembers(others);
      setChosen(new Set(others.map(m => m.id)));
    }).catch(e => setErr(e?.message || "could not load members"));
  }, [me]);

  async function start() {
    setBusy(true);
    setErr(null);
    try {
      const id = await createAndStart({ title: title.trim(), kind, participants: [...chosen] });
      onStarted?.(id);
      onClose();
    } catch (e: any) {
      setErr(e?.message || "could not start");
      setBusy(false);
    }
  }

  const stop = (e: React.SyntheticEvent) => e.stopPropagation();
  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 p-4"
         onPointerDown={stop} onPointerUp={stop} onClick={onClose}>
      <div onClick={stop}
           className={cn("w-full max-w-md rounded-2xl border p-5 shadow-xl",
                         dark ? "bg-zinc-900 text-white border-white/15" : "bg-card text-foreground border-border")}>
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-lg font-semibold flex items-center gap-2"><Mic className="w-5 h-5 text-red-500" /> Record</div>
            <p className={cn("text-xs mt-1", dark ? "text-white/60" : "text-muted-foreground")}>
              Everyone you tick will see the transcript and the report; nobody else. Tell the table you are recording.
            </p>
          </div>
          <button onClick={onClose} className="p-1 rounded-full hover:bg-white/10" aria-label="Close"><X className="w-4 h-4" /></button>
        </div>

        <div className="mt-4 flex gap-2">
          {KINDS.map(k => (
            <button key={k.id} onClick={() => setKind(k.id)}
                    className={cn("px-3 py-1.5 rounded-full text-sm border",
                                  kind === k.id ? "bg-red-500 text-white border-red-500" : dark ? "border-white/20 hover:bg-white/10" : "border-border hover:bg-muted")}>
              {k.label}
            </button>
          ))}
        </div>

        <input value={title} onChange={e => setTitle(e.target.value)} placeholder="Title (optional)"
               className={cn("mt-3 w-full rounded-lg border px-3 py-2 text-sm bg-transparent",
                             dark ? "border-white/20 placeholder:text-white/40" : "border-border")} />

        <div className="mt-4 text-xs uppercase tracking-wider font-semibold opacity-70">Who is here</div>
        {!members && !err && <Loader2 className="w-4 h-4 animate-spin mt-2" />}
        {members && members.length === 0 && <p className="text-sm mt-2 opacity-70">Only you. The recording stays yours.</p>}
        <div className="mt-2 grid grid-cols-2 gap-2">
          {members?.map(m => {
            const on = chosen.has(m.id);
            return (
              <button key={m.id}
                      onClick={() => setChosen(prev => { const n = new Set(prev); on ? n.delete(m.id) : n.add(m.id); return n; })}
                      className={cn("flex items-center gap-2 rounded-lg border px-3 py-2 text-sm text-left",
                                    on ? "border-red-500 bg-red-500/10" : dark ? "border-white/15 hover:bg-white/5" : "border-border hover:bg-muted")}>
                <span className={cn("w-4 h-4 rounded border flex items-center justify-center text-[10px]",
                                    on ? "bg-red-500 border-red-500 text-white" : "border-current opacity-50")}>{on ? "✓" : ""}</span>
                <span className="truncate">{m.name}</span>
              </button>
            );
          })}
        </div>

        {err && <p className="mt-3 text-sm text-red-400">{err}</p>}

        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className={cn("px-4 py-2 rounded-lg text-sm", dark ? "hover:bg-white/10" : "hover:bg-muted")}>Cancel</button>
          <button onClick={start} disabled={busy || !members}
                  className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold bg-red-500 text-white hover:bg-red-400 disabled:opacity-50">
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Mic className="w-4 h-4" />} Start recording
          </button>
        </div>
      </div>
    </div>
  );
}
