/**
 * Grey placeholder rows in the shape of the list that is loading, so a
 * screen keeps its layout while data arrives instead of showing a lone
 * spinner. Chat, Documents and Email draw theirs inline; this is the
 * shared one for the rest.
 */
import { cn } from "@/lib/utils";

export function ListSkeleton({ rows = 5, avatar = false, className }: {
  rows?: number;
  /** a round leading placeholder, for lists of people */
  avatar?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2 py-2", className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 px-3 py-3 rounded-xl border border-border/60 animate-pulse">
          {avatar
            ? <div className="w-8 h-8 rounded-full bg-muted/70 shrink-0" />
            : <div className="w-4 h-4 rounded bg-muted/70 shrink-0" />}
          <div className="flex-1 space-y-2">
            <div className="h-3 rounded bg-muted/70" style={{ width: `${55 + ((i * 17) % 35)}%` }} />
            <div className="h-2.5 rounded bg-muted/40 w-1/3" />
          </div>
        </div>
      ))}
    </div>
  );
}
