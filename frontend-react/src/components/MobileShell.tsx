/**
 * Mobile-shell helpers for the three-pane apps.
 *
 * The desktop pattern is `aside | section | aside` with both asides at
 * fixed 280-340px and content in the middle. Below 768px (Tailwind `md`)
 * that crowds out everything. This module converts the layout for small
 * screens without forcing every app to rewrite its JSX:
 *
 *   - `useTriPane()` — local state for "which drawer is open" + ESC
 *     handler. Each app calls it once.
 *   - `<MobileTopBar>` — slim 48px bar visible only `md:hidden`, with
 *     hamburger on the left and an optional PanelRight on the right.
 *     Sits at the top of the center section. Title is optional.
 *   - `mobileAsideLeft(open)` / `mobileAsideRight(open)` — class strings
 *     each app drops onto its aside elements. Below `md` the aside is a
 *     fixed drawer that slides in from the relevant edge; at `md+` it
 *     reverts to its normal in-flow position. Width-on-md stays whatever
 *     the app already used because we let the caller add `md:w-[...]`
 *     in addition to ours.
 *   - `<MobileBackdrop>` — dimmed overlay that closes the drawer on tap.
 *
 * All the desktop classes (`md:` and up) the app already has continue to
 * apply unchanged. Net effect: keep existing layouts on desktop, get a
 * usable phone view essentially for free.
 */

import { useCallback, useEffect, useState } from "react";
import { ChevronLeft, Menu, PanelRight, X } from "lucide-react";
import { cn } from "@/lib/utils";

export interface TriPane {
  leftOpen: boolean;
  rightOpen: boolean;
  setLeftOpen: (v: boolean) => void;
  setRightOpen: (v: boolean) => void;
  toggleLeft: () => void;
  toggleRight: () => void;
  closeAll: () => void;
}

export function useTriPane(): TriPane {
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);

  const closeAll = useCallback(() => {
    setLeftOpen(false);
    setRightOpen(false);
  }, []);

  useEffect(() => {
    function esc(e: KeyboardEvent) { if (e.key === "Escape") closeAll(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [closeAll]);

  // Lock body scroll while a mobile drawer is open so the page behind
  // doesn't jiggle when the user drags.
  useEffect(() => {
    const anyOpen = leftOpen || rightOpen;
    if (anyOpen) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => { document.body.style.overflow = prev; };
    }
  }, [leftOpen, rightOpen]);

  return {
    leftOpen, rightOpen,
    setLeftOpen, setRightOpen,
    toggleLeft:  useCallback(() => setLeftOpen(v => !v), []),
    toggleRight: useCallback(() => setRightOpen(v => !v), []),
    closeAll,
  };
}

/** Tailwind classes that turn an existing left aside into a sliding
 *  drawer below `md` while leaving the desktop layout alone. */
export function mobileAsideLeft(open: boolean): string {
  return cn(
    // Mobile: full-height fixed drawer, slide from left edge.
    "max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-40 max-md:w-[280px]",
    "max-md:shadow-2xl max-md:transition-transform",
    open ? "max-md:translate-x-0" : "max-md:-translate-x-full",
  );
}

/** Mirror of `mobileAsideLeft` for the right context pane. */
export function mobileAsideRight(open: boolean): string {
  return cn(
    "max-md:fixed max-md:inset-y-0 max-md:right-0 max-md:z-40 max-md:w-[300px]",
    "max-md:shadow-2xl max-md:transition-transform",
    open ? "max-md:translate-x-0" : "max-md:translate-x-full",
  );
}

interface MobileTopBarProps {
  title?: React.ReactNode;
  /** Opens the left drawer; apps without one (Home, Tasks) leave it out. */
  onMenuClick?: () => void;
  /** A "‹" at the left instead of the menu — a screen stacked on top of
   *  a list (WhatsApp thread over the chat list). Wins over onMenuClick. */
  onBack?: () => void;
  backLabel?: string;
  onContextClick?: () => void;
  contextLabel?: string;
  /** One app action at the right, e.g. the calendar's quick-add. */
  rightAction?: React.ReactNode;
  /** A thin row under the bar (a search pill, the calendar's day/week
   *  switch): part of the same sticky block, 36 px, no second "bar". */
  below?: React.ReactNode;
  /** Home shows its name at the left like an app's own screen; everything
   *  else centres the title. */
  titleAlign?: "center" | "left";
}

/** The id NotificationBell portals its button into on a phone, so the
 *  bell sits in the bar instead of floating over the content. */
export const MOBILE_BELL_SLOT_ID = "yorik-mobile-bell-slot";

/** The one app bar every screen shares on a phone: 44 px (plus the
 *  status bar), menu or nothing at the left, the title in the middle,
 *  at most one app action and the bell at the right. Before 2026-10-01
 *  every app stacked its own header under this one (the calendar had
 *  170 px of chrome before the first appointment) and the bell floated
 *  as a separate circle. */
export function MobileTopBar({
  title, onMenuClick, onBack, backLabel = "Back", onContextClick, contextLabel = "Details", rightAction, below,
  titleAlign = "center",
}: MobileTopBarProps) {
  return (
    <div className="md:hidden sticky top-0 z-30 shrink-0 border-b border-border bg-background/90 backdrop-blur pt-[env(safe-area-inset-top)]">
      <div className="h-11 pl-[max(0.25rem,env(safe-area-inset-left))] pr-[max(0.25rem,env(safe-area-inset-right))] flex items-center gap-0.5">
        {onBack ? (
          <button
            onClick={onBack}
            aria-label={backLabel}
            className="w-11 h-11 rounded-md text-foreground flex items-center justify-center shrink-0"
          >
            <ChevronLeft className="w-7 h-7 -ml-1" />
          </button>
        ) : onMenuClick ? (
          <button
            onClick={onMenuClick}
            aria-label="Open menu"
            className="w-11 h-11 rounded-md text-muted-foreground hover:text-foreground flex items-center justify-center shrink-0"
          >
            <Menu className="w-[22px] h-[22px]" />
          </button>
        ) : (
          <span className="w-2 shrink-0" />
        )}
        <div className={cn("flex-1 min-w-0 text-[17px] font-semibold tracking-tight truncate",
                            titleAlign === "left" ? "text-left pl-2" : "text-center")}>
          {title}
        </div>
        <div className="flex items-center shrink-0">
          {rightAction}
          {onContextClick && (
            <button
              onClick={onContextClick}
              aria-label={contextLabel}
              className="w-11 h-11 rounded-md text-muted-foreground hover:text-foreground flex items-center justify-center shrink-0"
            >
              <PanelRight className="w-[22px] h-[22px]" />
            </button>
          )}
          <span id={MOBILE_BELL_SLOT_ID} className="flex items-center shrink-0" />
        </div>
      </div>
      {below && (
        <div className="h-9 px-3 pb-1 flex items-center gap-2">
          {below}
        </div>
      )}
    </div>
  );
}

/** Dimmed full-screen overlay shown beneath a mobile drawer. Tap = close. */
export function MobileBackdrop({ show, onClick }:
  { show: boolean; onClick: () => void }) {
  if (!show) return null;
  return (
    <div
      className="md:hidden fixed inset-0 z-30 bg-black/40 backdrop-blur-sm"
      onClick={onClick}
      aria-hidden="true"
    />
  );
}

/** Small ✕ button asides can put inside themselves so users can close
 *  the drawer from inside (visible only on mobile). */
export function MobileDrawerClose({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      aria-label="Close menu"
      className="md:hidden absolute top-2 right-2 w-8 h-8 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center justify-center z-10"
    >
      <X className="w-4 h-4" />
    </button>
  );
}
