/**
 * PersonAvatar — one way to show a household member everywhere: their
 * photo, or their initials on their colour. Pure; callers resolve the
 * person (lib/people.ts) and pass colour + photo.
 */
import type { CSSProperties } from "react";
import { cn } from "@/lib/utils";

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
