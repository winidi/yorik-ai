/**
 * Family board on any screen (/board): the wall tablet's view for the
 * phone and the desktop. Same feed, same component; the signed-in
 * person ticks their own tiles directly, no PIN picker needed here.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { CalendarDays, GraduationCap, LayoutGrid, ListChecks } from "lucide-react";
import { useAuth } from "@/components/AuthGate";
import { Dock } from "@/components/Dock";
import { FamilyBoard, type BoardMode } from "@/apps/ambient/FamilyBoard";
import { cn } from "@/lib/utils";

/** `label` is a translation key. */
const MODES: Array<{ id: BoardMode; label: string; Icon: typeof LayoutGrid }> = [
  { id: "board", label: "board.wall.board", Icon: LayoutGrid },
  { id: "calendar", label: "board.wall.calendar", Icon: CalendarDays },
  { id: "tasks", label: "board.wall.tasks", Icon: ListChecks },
  { id: "timetable", label: "board.wall.timetable", Icon: GraduationCap },
];

export function BoardApp() {
  const auth = useAuth();
  const { t } = useTranslation();
  const meId: string | null = (auth.user as any)?.id || null;
  const [mode, setMode] = useState<BoardMode>(() => {
    try { return (localStorage.getItem("yorik:board:mode") as BoardMode) || "board"; } catch { return "board"; }
  });
  function pick(m: BoardMode) { setMode(m); try { localStorage.setItem("yorik:board:mode", m); } catch {} }

  return (
    <div className="h-screen overflow-y-auto bg-[#fbfaf7]">
      <div className="relative min-h-full md:h-full md:pb-24">
        <div className="relative min-h-full md:h-full">
          <FamilyBoard mode={mode} currentUserId={meId} currentUserRole={(auth.user as any)?.role || null} lockOthers offerJoin onNeedSignIn={() => {}} />
        </div>
      </div>
      <div className="fixed right-20 md:right-6 bottom-[calc(var(--dock-clearance)+1rem)] md:bottom-6 z-30 flex gap-1 rounded-full bg-white/95 border border-[#e9e6df] shadow-lg p-1">
        {MODES.map(m => (
          <button key={m.id} onClick={() => pick(m.id)} title={t(m.label)}
                  className={cn("flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-semibold",
                                mode === m.id ? "bg-[#1f2430] text-white" : "text-[#1f2430] hover:bg-[#f1efe9]")}>
            <m.Icon className="w-3.5 h-3.5" /> <span className={cn(mode !== m.id && "hidden sm:inline")}>{t(m.label)}</span>
          </button>
        ))}
      </div>
      <Dock activeAppId="board" />
    </div>
  );
}
