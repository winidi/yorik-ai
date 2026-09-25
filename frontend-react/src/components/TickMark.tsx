/**
 * The to-do checkbox: an empty rounded box, or filled in the signed-in
 * person's colour with a white check. It pops once when it changes to
 * done in front of you (not when a list loads already ticked).
 */
import { useEffect, useRef, useState } from "react";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAuth } from "@/components/AuthGate";
import { usePerson } from "@/lib/people";

export function TickMark({ done, size = 18, className }: { done: boolean; size?: number; className?: string }) {
  const me = useAuth().user;
  const person = usePerson(me ? String(me.id) : null);
  const colour = person?.color || "var(--color-primary)";
  const was = useRef(done);
  const [pop, setPop] = useState(false);
  useEffect(() => {
    if (done && !was.current) setPop(true);
    was.current = done;
  }, [done]);
  return (
    <span
      className={cn(
        "inline-flex items-center justify-center rounded-md border-2 transition-colors",
        done ? "border-transparent text-white" : "border-muted-foreground/50 hover:border-foreground",
        pop && "tick-pop",
        className,
      )}
      style={{ width: size, height: size, background: done ? colour : "transparent" }}
      onAnimationEnd={() => setPop(false)}
    >
      {done && <Check className="w-[70%] h-[70%]" strokeWidth={3} />}
    </span>
  );
}
