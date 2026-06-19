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
import { Menu, PanelRight, X } from "lucide-react";
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
  onMenuClick: () => void;
  onContextClick?: () => void;
  contextLabel?: string;
}

export function MobileTopBar({
  title, onMenuClick, onContextClick, contextLabel = "Details",
}: MobileTopBarProps) {
  return (
    <div className="md:hidden h-12 px-3 border-b border-border bg-background/85 backdrop-blur flex items-center gap-2 shrink-0 sticky top-0 z-30">
      <button
        onClick={onMenuClick}
        aria-label="Open menu"
        className="w-9 h-9 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center justify-center shrink-0"
      >
        <Menu className="w-4 h-4" />
      </button>
      <div className="flex-1 min-w-0 text-sm font-medium truncate text-center">
        {title}
      </div>
      {onContextClick ? (
        <button
          onClick={onContextClick}
          aria-label={contextLabel}
          className="w-9 h-9 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition flex items-center justify-center shrink-0"
        >
          <PanelRight className="w-4 h-4" />
        </button>
      ) : (
        <span className="w-9 shrink-0" />
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
