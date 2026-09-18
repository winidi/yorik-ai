/**
 * Family board on any screen (/board): the wall tablet's view for the
 * phone and the desktop. Same feed, same component; the signed-in
 * person ticks their own tiles directly, no PIN picker needed here.
 */
import { useState } from "react";
import { CalendarDays, LayoutGrid, ListChecks } from "lucide-react";
import { useAuth } from "@/components/AuthGate";
import { Dock } from "@/components/Dock";
import { FamilyBoard, type BoardMode } from "@/apps/ambient/FamilyBoard";
import { cn } from "@/lib/utils";

const MODES: Array<{ id: BoardMode; label: string; Icon: typeof LayoutGrid }> = [
  { id: "board", label: "Tafel", Icon: LayoutGrid },
  { id: "calendar", label: "Kalender", Icon: CalendarDays },
  { id: "tasks", label: "Aufgaben", Icon: ListChecks },
];

export function BoardApp() {
  const auth = useAuth();
  const meId: string | null = (auth.user as any)?.id || null;
  const [mode, setMode] = useState<BoardMode>(() => {
    try { return (localStorage.getItem("yorik:board:mode") as BoardMode) || "board"; } catch { return "board"; }
  });
  function pick(m: BoardMode) { setMode(m); try { localStorage.setItem("yorik:board:mode", m); } catch {} }

  return (
    <div className="h-screen overflow-hidden bg-[#fbfaf7]">
      <div className="relative h-full pb-24">
        <div className="relative h-full">
          <FamilyBoard mode={mode} currentUserId={meId} onNeedSignIn={() => {}} />
        </div>
      </div>
      <div className="fixed right-16 top-3 z-30 flex gap-1 rounded-full bg-white/90 border border-[#e9e6df] shadow p-1">
        {MODES.map(m => (
          <button key={m.id} onClick={() => pick(m.id)} title={m.label}
                  className={cn("flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-semibold",
                                mode === m.id ? "bg-[#1f2430] text-white" : "text-[#1f2430] hover:bg-[#f1efe9]")}>
            <m.Icon className="w-3.5 h-3.5" /> {m.label}
          </button>
        ))}
      </div>
      <Dock activeAppId="board" />
    </div>
  );
}
