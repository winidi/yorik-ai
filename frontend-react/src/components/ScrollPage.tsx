/**
 * A full-screen page that scrolls on its own.
 *
 * The body does not scroll and allows no touch pan (index.css), so a
 * page that relies on the document scrolling cannot be moved with a
 * finger once it is taller than the screen: its last button stays out
 * of reach on a phone. Entry screens (login, setup, join, onboarding,
 * the diagnostics question, the error page) are wrapped in this.
 */
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function ScrollPage({ className, innerClassName, children }: {
  /** Background and text colour of the whole screen. */
  className?: string;
  /** Alignment and padding of the content. */
  innerClassName?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("h-screen overflow-y-auto", className)}>
      <div className={cn("min-h-full flex justify-center", innerClassName)}>
        {children}
      </div>
    </div>
  );
}
