/**
 * PersonAvatar — one way to show a household member everywhere: their
 * photo, or their initials on their colour. Pure; callers resolve the
 * person (lib/people.ts) and pass colour + photo.
 */
import type { CSSProperties } from "react";
import { cn } from "@/lib/utils";
import { usePerson } from "@/lib/people";

export function initialsOf(name: string): string {
  return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map(s => s[0]).join("").toUpperCase();
}

export function PersonAvatar({ name, color, avatarUrl, size = 32, className, initials, fallbackStyle, title }: {
  name: string;
  color?: string | null;
  avatarUrl?: string | null;
  size?: number;
  className?: string;
  initials?: string;
  /** used when no colour is known (e.g. a name that is not a member) */
  fallbackStyle?: CSSProperties;
  title?: string;
}) {
  const style: CSSProperties = color
    ? { background: color, color: "#fff" }
    : (fallbackStyle || { background: "hsl(220 10% 60% / 0.25)" });
  return (
    <span
      className={cn("inline-flex items-center justify-center rounded-full overflow-hidden font-semibold shrink-0 select-none", className)}
      style={{ width: size, height: size, fontSize: Math.max(9, Math.round(size * 0.42)), ...style }}
      title={title || name}
      aria-label={name}
    >
      {avatarUrl
        ? <img src={avatarUrl} alt="" className="w-full h-full object-cover" draggable={false} />
        : (initials || initialsOf(name))}
    </span>
  );
}

/** A household member by id or name: their photo or initials on their
 *  colour. Names that aren't members get the neutral fallback. */
export function MemberAvatar({ who, name, size = 20, className }: {
  who: string | null | undefined;
  name?: string;
  size?: number;
  className?: string;
}) {
  const p = usePerson(who || name);
  const label = p?.first_name || p?.name || name || who || "?";
  return (
    <PersonAvatar name={p?.name || label} color={p?.color} avatarUrl={p?.avatar_url}
                  size={size} className={className} title={label} />
  );
}

/** Overlapping faces for everyone a thing belongs to (tasks, events). */
export function MemberStack({ people, size = 20, max = 3 }: {
  people: Array<{ id?: string | null; name?: string | null }>;
  size?: number;
  max?: number;
}) {
  const shown = people.slice(0, max);
  const rest = people.length - shown.length;
  return (
    <span className="inline-flex items-center" title={people.map(p => p.name).filter(Boolean).join(", ")}>
      {shown.map((p, i) => (
        <MemberAvatar key={(p.id || p.name || "") + i} who={p.id || p.name} name={p.name || undefined} size={size}
                      className={cn("ring-2 ring-background", i > 0 && "-ml-1.5")} />
      ))}
      {rest > 0 && <span className="ml-1 text-2xs text-muted-foreground">+{rest}</span>}
    </span>
  );
}
